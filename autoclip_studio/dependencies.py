"""Centralized dependency manifest, detection, reuse, download, and model management."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .captions import TEMPLATES, _installed_fonts
from .paths import APP_ROOT, DATA_ROOT, LOG_DIR, MODEL_CACHE
from .speech_engine import (
    MODELS, SpeechSettings, complete_model, cuda_libraries, download_speech_model,
    find_executable, find_model, gpu_info, model_cache_root, python_cuda, subtitle_edit_engine,
)


MANIFEST_PATH = APP_ROOT / "scripts" / "dependencies.json"
REQUIREMENTS_PATH = APP_ROOT / "requirements.txt"


@dataclass(frozen=True, slots=True)
class DependencySpec:
    id: str
    name: str
    version: str
    category: str
    install_path: str
    download_url: str
    sha256: str
    download_size_bytes: int | None
    estimated_installed_bytes: int
    mandatory: bool
    hardware_requirement: str
    integrity_method: str
    fallback_url: str = ""


@dataclass(frozen=True, slots=True)
class DependencyStatus:
    spec: DependencySpec
    state: str                 # Ready | Reused existing | Missing | Corrupted | Optional
    installed_version: str
    resolved_path: str
    storage_bytes: int
    update_available: bool
    detail: str
    reused_external: bool = False


@dataclass(frozen=True, slots=True)
class DownloadMetrics:
    downloaded_bytes: int
    total_bytes: int | None
    speed_bps: float
    eta_seconds: float | None


def format_bytes(num_bytes: int | float | None) -> str:
    if num_bytes is None:
        return "Unknown"
    value = float(max(0, num_bytes))
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024.0 or unit == "GB":
            return f"{value:.0f} {unit}" if unit in {"B", "KB"} else f"{value:.2f} {unit}" if unit == "GB" else f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.2f} GB"


def directory_size(path: Path) -> int:
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    if not path.is_dir():
        return 0
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            try:
                total += item.stat().st_size
            except OSError:
                pass
    return total


def available_disk_bytes(path: Path = APP_ROOT) -> int:
    target = path if path.exists() else path.parent
    while not target.exists() and target != target.parent:
        target = target.parent
    return int(shutil.disk_usage(target).free)


def load_manifest(path: Path = MANIFEST_PATH) -> tuple[list[DependencySpec], list[dict[str, Any]], str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    specs = [
        DependencySpec(
            id=item["id"],
            name=item["name"],
            version=str(item["version"]),
            category=item["category"],
            install_path=str(item.get("install_path", "")),
            download_url=str(item.get("download_url", "")),
            sha256=str(item.get("sha256", "")),
            download_size_bytes=item.get("download_size_bytes"),
            estimated_installed_bytes=int(item.get("estimated_installed_bytes") or 0),
            mandatory=bool(item.get("mandatory", False)),
            hardware_requirement=str(item.get("hardware_requirement", "any")),
            integrity_method=str(item.get("integrity_method", "version_probe")),
            fallback_url=str(item.get("fallback_url", "")),
        )
        for item in payload.get("components", [])
    ]
    return specs, list(payload.get("whisper_models", [])), str(payload.get("default_whisper_model", "large-v3-turbo"))


def check_disk_space(required_bytes: int, target: Path = APP_ROOT) -> tuple[bool, int]:
    free = available_disk_bytes(target)
    return free >= required_bytes, free


def download_resumable(
    url: str,
    target: Path,
    *,
    expected_sha256: str = "",
    cancel_event: threading.Event | None = None,
    pause_event: threading.Event | None = None,
    on_progress: Callable[[DownloadMetrics], None] | None = None,
    max_retries: int = 3,
    timeout: int = 30,
) -> Path:
    """Download `url` to `target` with HTTP Range resume, pause/cancel, exponential backoff, and SHA-256 check."""
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_suffix(target.suffix + ".part")

    def _sha256(path: Path) -> str:
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest().lower()

    if expected_sha256 and target.is_file() and _sha256(target) == expected_sha256.lower():
        if on_progress:
            size = target.stat().st_size
            on_progress(DownloadMetrics(size, size, 0.0, 0.0))
        return target

    for attempt in range(max_retries):
        if cancel_event and cancel_event.is_set():
            raise RuntimeError("Download cancelled")
        existing = part.stat().st_size if part.is_file() else 0
        headers = {"User-Agent": "AutoClipStudio-Setup/1.0"}
        if existing > 0:
            headers["Range"] = f"bytes={existing}-"

        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status = getattr(response, "status", 200)
                if existing > 0 and status == 200:
                    # Server ignored Range; restart partial file cleanly.
                    existing = 0
                content_length = response.headers.get("Content-Length")
                remaining = int(content_length) if content_length and content_length.isdigit() else None
                total_bytes = (existing + remaining) if remaining is not None else None
                if total_bytes is not None:
                    enough, free = check_disk_space(max(0, total_bytes - existing), target.parent)
                    if not enough:
                        raise RuntimeError(
                            f"Insufficient disk space: need {format_bytes(total_bytes - existing)}, "
                            f"only {format_bytes(free)} available."
                        )

                mode = "ab" if existing > 0 and status == 206 else "wb"
                downloaded = existing
                started = time.perf_counter()
                last_report = 0.0
                with part.open(mode) as out:
                    while True:
                        if cancel_event and cancel_event.is_set():
                            raise RuntimeError("Download cancelled")
                        while pause_event and pause_event.is_set():
                            if cancel_event and cancel_event.is_set():
                                raise RuntimeError("Download cancelled")
                            time.sleep(0.1)
                        chunk = response.read(65536)
                        if not chunk:
                            break
                        out.write(chunk)
                        downloaded += len(chunk)
                        now = time.perf_counter()
                        if on_progress and (now - last_report >= 0.1 or (total_bytes and downloaded >= total_bytes)):
                            elapsed = max(0.001, now - started)
                            speed = (downloaded - existing) / elapsed
                            eta = ((total_bytes - downloaded) / speed) if (total_bytes and speed > 0) else None
                            on_progress(DownloadMetrics(downloaded, total_bytes, speed, eta))
                            last_report = now

            if expected_sha256:
                actual = _sha256(part)
                if actual != expected_sha256.lower():
                    part.unlink(missing_ok=True)
                    raise RuntimeError(
                        f"SHA-256 verification failed for {target.name} (expected {expected_sha256[:12]}, got {actual[:12]})."
                    )
            part.replace(target)
            return target
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt + 1 >= max_retries:
                raise
            time.sleep(2 ** attempt)
    return target


def _pinned_requirements() -> dict[str, str]:
    pins: dict[str, str] = {}
    if not REQUIREMENTS_PATH.is_file():
        return pins
    for line in REQUIREMENTS_PATH.read_text(encoding="utf-8").splitlines():
        req, _, marker = line.partition(";")
        if 'sys_platform == "win32"' in marker and sys.platform != "win32":
            continue
        name, sep, ver = req.strip().partition("==")
        if sep:
            pins[name.strip()] = ver.strip()
    return pins


def inspect_all_dependencies(
    manifest_path: Path = MANIFEST_PATH,
    *,
    speech_settings: SpeechSettings | None = None,
    cloud_only: bool = False,
) -> list[DependencyStatus]:
    """Inspect all components in the manifest, reusing existing installations safely."""
    specs, _, default_model = load_manifest(manifest_path)
    speech_settings = speech_settings or SpeechSettings()
    pins = _pinned_requirements()
    gpu = gpu_info()
    has_gpu = gpu is not None
    statuses: list[DependencyStatus] = []

    for spec in specs:
        if spec.id == "uv":
            uv_path = APP_ROOT / "tools" / "uv.exe"
            if not uv_path.is_file():
                which_uv = shutil.which("uv")
                if which_uv:
                    statuses.append(DependencyStatus(spec, "Reused existing", spec.version, which_uv, 0, False, "System uv detected", True))
                    continue
                statuses.append(DependencyStatus(spec, "Missing", "—", str(uv_path), 0, False, "uv.exe not installed"))
            else:
                try:
                    out = subprocess.run(
                        [str(uv_path), "--version"], capture_output=True, text=True, timeout=5,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                    ).stdout.strip()
                    if out.startswith("uv "):
                        uv_ver = out.split()[1]
                        statuses.append(DependencyStatus(spec, "Ready", uv_ver, str(uv_path), uv_path.stat().st_size, uv_ver != spec.version, "Pinned bootstrapper verified"))
                    else:
                        statuses.append(DependencyStatus(spec, "Corrupted", "unknown", str(uv_path), uv_path.stat().st_size, False, "uv.exe failed version check"))
                except (OSError, subprocess.SubprocessError, IndexError):
                    statuses.append(DependencyStatus(spec, "Corrupted", "unknown", str(uv_path), uv_path.stat().st_size, False, "uv.exe could not run"))

        elif spec.id == "python_runtime":
            ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
            py_exe = APP_ROOT / ".venv" / "Scripts" / "python.exe"
            pyw_exe = APP_ROOT / ".venv" / "Scripts" / "pythonw.exe"
            rt_dir = APP_ROOT / ".runtime"
            if (3, 11) <= sys.version_info[:2] < (3, 13) and (py_exe.is_file() or pyw_exe.is_file()):
                reused = not rt_dir.exists()
                statuses.append(DependencyStatus(
                    spec, "Reused existing" if reused else "Ready", ver,
                    sys.executable, directory_size(rt_dir), False,
                    "Compatible Python runtime active", reused,
                ))
            else:
                statuses.append(DependencyStatus(spec, "Missing", ver, str(rt_dir), 0, False, "Python 3.11/3.12 runtime required"))

        elif spec.id == "python_venv":
            venv = APP_ROOT / ".venv"
            pyw = venv / "Scripts" / "pythonw.exe"
            if venv.is_dir() and pyw.is_file():
                statuses.append(DependencyStatus(spec, "Ready", spec.version, str(venv), directory_size(venv), False, "Project virtual environment active"))
            else:
                statuses.append(DependencyStatus(spec, "Missing", "—", str(venv), 0, False, "Virtual environment missing"))

        elif spec.id == "pyside6":
            expected = pins.get("PySide6", spec.version)
            try:
                inst = importlib.metadata.version("PySide6")
                state = "Ready" if inst == expected else "Ready"
                upd = inst != expected
                p = APP_ROOT / ".venv" / "Lib" / "site-packages" / "PySide6"
                statuses.append(DependencyStatus(spec, state, inst, str(p), directory_size(p), upd, f"Qt GUI runtime {inst}"))
            except importlib.metadata.PackageNotFoundError:
                statuses.append(DependencyStatus(spec, "Missing", "—", spec.install_path, 0, False, "PySide6 not installed"))

        elif spec.id == "python_packages":
            missing, mismatched = [], []
            for pkg, exp in pins.items():
                try:
                    inst = importlib.metadata.version(pkg)
                    if inst != exp:
                        mismatched.append(f"{pkg}=={inst} (need {exp})")
                except importlib.metadata.PackageNotFoundError:
                    missing.append(pkg)
            site_dir = APP_ROOT / ".venv" / "Lib" / "site-packages"
            if missing:
                statuses.append(DependencyStatus(spec, "Missing", f"{len(pins) - len(missing)}/{len(pins)}", str(site_dir), directory_size(site_dir), False, "Missing: " + ", ".join(missing)))
            elif mismatched:
                statuses.append(DependencyStatus(spec, "Ready", f"{len(pins)}/{len(pins)}", str(site_dir), directory_size(site_dir), True, "Version diff: " + ", ".join(mismatched)))
            else:
                statuses.append(DependencyStatus(spec, "Ready", f"{len(pins)}/{len(pins)} pinned", str(site_dir), directory_size(site_dir), False, "All pinned packages verified"))

        elif spec.id == "ffmpeg":
            ff_dir = APP_ROOT / "tools" / "ffmpeg"
            ff_exe = ff_dir / "bin" / "ffmpeg.exe"
            fp_exe = ff_dir / "bin" / "ffprobe.exe"
            if ff_exe.is_file() and fp_exe.is_file():
                try:
                    out = subprocess.run([str(ff_exe), "-version"], capture_output=True, text=True, timeout=8,
                                         creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0).stdout
                    ver_line = out.splitlines()[0].split()[2] if out.startswith("ffmpeg version ") else "unknown"
                    statuses.append(DependencyStatus(spec, "Ready", ver_line, str(ff_dir), directory_size(ff_dir), False, "Project FFmpeg & FFprobe verified"))
                except (OSError, subprocess.SubprocessError, IndexError):
                    statuses.append(DependencyStatus(spec, "Corrupted", "—", str(ff_dir), directory_size(ff_dir), False, "FFmpeg failed executable check"))
            else:
                sys_ff = shutil.which("ffmpeg")
                sys_fp = shutil.which("ffprobe")
                if sys_ff and sys_fp:
                    statuses.append(DependencyStatus(spec, "Reused existing", "system", str(Path(sys_ff).parent), 0, False, "Compatible system FFmpeg detected", True))
                else:
                    statuses.append(DependencyStatus(spec, "Missing", "—", str(ff_dir), 0, False, "FFmpeg & FFprobe missing"))

        elif spec.id == "yt_dlp":
            expected = pins.get("yt-dlp", spec.version)
            pkg_exe = APP_ROOT / "tools" / "package" / "yt-dlp.exe"
            try:
                inst = importlib.metadata.version("yt-dlp")
                statuses.append(DependencyStatus(spec, "Ready", inst, str(APP_ROOT / ".venv" / "Lib" / "site-packages" / "yt_dlp"), 0, inst != expected, f"yt-dlp {inst} active"))
            except importlib.metadata.PackageNotFoundError:
                if pkg_exe.is_file():
                    statuses.append(DependencyStatus(spec, "Reused existing", "bundled", str(pkg_exe), pkg_exe.stat().st_size, False, "Bundled yt-dlp.exe", True))
                else:
                    statuses.append(DependencyStatus(spec, "Missing", "—", spec.install_path, 0, False, "yt-dlp missing"))

        elif spec.id == "opencv_face_model":
            try:
                import cv2
                inst = importlib.metadata.version("opencv-python-headless")
                cascade = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
                if cascade.is_file():
                    statuses.append(DependencyStatus(spec, "Ready", inst, str(cascade), cascade.stat().st_size, False, "OpenCV + Haar cascade ready"))
                else:
                    statuses.append(DependencyStatus(spec, "Corrupted", inst, str(cascade), 0, False, "Haar cascade XML missing"))
            except Exception:
                statuses.append(DependencyStatus(spec, "Missing", "—", spec.install_path, 0, False, "OpenCV missing"))

        elif spec.id == "whisper_engine":
            sub_exe = subtitle_edit_engine()
            custom_exe = find_executable(speech_settings.executable)
            try:
                fw_ver = importlib.metadata.version("faster-whisper")
            except importlib.metadata.PackageNotFoundError:
                fw_ver = ""
            if sub_exe:
                statuses.append(DependencyStatus(
                    spec, "Reused existing", "Purfview XXL (Subtitle Edit)",
                    str(sub_exe), 0, False,
                    f"Reusing Subtitle Edit engine (+ Python faster-whisper {fw_ver or 'missing'})", True,
                ))
            elif custom_exe and custom_exe.is_file():
                statuses.append(DependencyStatus(spec, "Ready", "Purfview XXL", str(custom_exe), custom_exe.stat().st_size, False, "Standalone Purfview XXL ready"))
            elif fw_ver:
                statuses.append(DependencyStatus(spec, "Ready", f"faster-whisper {fw_ver}", str(APP_ROOT / ".venv" / "Lib" / "site-packages" / "faster_whisper"), 0, False, "Python Faster-Whisper ready"))
            else:
                statuses.append(DependencyStatus(spec, "Missing", "—", spec.install_path, 0, False, "No local transcription engine"))

        elif spec.id == "cuda_runtime":
            sub_exe = find_executable(speech_settings.executable)
            xxl_libs = cuda_libraries(sub_exe) if sub_exe else ()
            py_cuda = python_cuda()
            if not has_gpu:
                statuses.append(DependencyStatus(spec, "Optional", "CPU mode", "—", 0, False, "No NVIDIA GPU detected; CUDA not required"))
            elif py_cuda or xxl_libs:
                detail = "Python CUDA ready" if py_cuda else f"Reused Purfview CUDA libs ({len(xxl_libs)} DLLs)"
                statuses.append(DependencyStatus(spec, "Reused existing" if xxl_libs and not py_cuda else "Ready", "CUDA 12 / cuDNN 9", str(sub_exe.parent if sub_exe else ""), 0, False, detail, bool(xxl_libs and not py_cuda)))
            else:
                statuses.append(DependencyStatus(spec, "Optional", "Unavailable", "—", 0, False, "NVIDIA GPU present; CPU fallback active unless CUDA DLLs or Purfview XXL installed"))

        elif spec.id == "whisper_model_large_v3_turbo":
            model_path = find_model(default_model, speech_settings, find_executable(speech_settings.executable))
            if model_path and complete_model(model_path):
                is_external = not model_path.resolve().is_relative_to(MODEL_CACHE.resolve()) if MODEL_CACHE.exists() else True
                statuses.append(DependencyStatus(
                    spec, "Reused existing" if is_external else "Ready", default_model,
                    str(model_path), directory_size(model_path), False,
                    "Reused external Whisper cache" if is_external else "Cached locally", is_external,
                ))
            elif cloud_only:
                statuses.append(DependencyStatus(spec, "Optional", "Not needed (Cloud)", str(MODEL_CACHE), 0, False, "Skipped for cloud-only transcription"))
            else:
                statuses.append(DependencyStatus(spec, "Optional", "Not downloaded", str(MODEL_CACHE), 0, False, "Recommended default model; download when using local STT"))

        elif spec.id == "caption_fonts":
            installed = _installed_fonts()
            needed = sorted({t.font for t in TEMPLATES.values()})
            present = [f for f in needed if f.lower() in installed]
            missing_fonts = [f for f in needed if f.lower() not in installed]
            detail = f"{len(present)}/{len(needed)} template fonts available"
            if missing_fonts:
                detail += f" (fallback used for: {', '.join(missing_fonts)})"
            statuses.append(DependencyStatus(spec, "Ready", f"{len(present)}/{len(needed)}", spec.install_path, 0, False, detail))

    return statuses


def inspect_whisper_models(speech_settings: SpeechSettings | None = None) -> list[dict[str, Any]]:
    """Inspect each Whisper model individually, distinguishing AutoClip cache from external Subtitle Edit caches."""
    speech_settings = speech_settings or SpeechSettings()
    exe = find_executable(speech_settings.executable)
    _, manifest_models, _ = load_manifest()
    size_map = {item["id"]: int(item.get("estimated_bytes") or 0) for item in manifest_models}
    rows: list[dict[str, Any]] = []
    for name in MODELS:
        path = find_model(name, speech_settings, exe)
        if path and complete_model(path):
            owned = MODEL_CACHE.exists() and path.resolve().is_relative_to(MODEL_CACHE.resolve())
            rows.append({
                "id": name,
                "installed": True,
                "owned": owned,
                "path": str(path),
                "size_bytes": directory_size(path),
                "source": "AutoClip cache" if owned else "External (Subtitle Edit / custom)",
            })
        else:
            rows.append({
                "id": name,
                "installed": False,
                "owned": False,
                "path": str(MODEL_CACHE),
                "size_bytes": size_map.get(name, 0),
                "source": "Not downloaded",
            })
    return rows


def remove_owned_whisper_model(name: str) -> bool:
    """Safely remove a model only if it resides inside AutoClip Studio's own MODEL_CACHE."""
    if name not in MODELS or not MODEL_CACHE.exists():
        return False
    cache_root = MODEL_CACHE.resolve()
    removed = False
    for candidate in (
        cache_root / f"faster-whisper-{name}",
        cache_root / name,
        cache_root / f"models--Systran--faster-whisper-{name}",
        cache_root / f"models--mobiuslabsgatti--faster-whisper-{name}",
    ):
        if candidate.exists() and candidate.resolve().is_relative_to(cache_root):
            shutil.rmtree(candidate, ignore_errors=True)
            removed = True
    return removed


def download_whisper_model_checked(name: str, speech_settings: SpeechSettings | None = None) -> Path:
    if name not in MODELS:
        raise ValueError(f"Unsupported Whisper model: {name}")
    _, manifest_models, _ = load_manifest()
    est = next((int(m.get("estimated_bytes") or 0) for m in manifest_models if m["id"] == name), 0)
    if est > 0:
        enough, free = check_disk_space(est, MODEL_CACHE)
        if not enough:
            raise RuntimeError(f"Insufficient disk space for {name}: need {format_bytes(est)}, have {format_bytes(free)}.")
    return download_speech_model(name, speech_settings or SpeechSettings())
