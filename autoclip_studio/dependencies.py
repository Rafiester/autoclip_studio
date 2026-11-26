"""Centralized dependency manifest, detection, reuse, download, version persistence, and model management."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import re
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
from .diagnostics import redact
from .paths import APP_ROOT, DATA_ROOT, LOG_DIR, MODEL_CACHE, bundled_binary
from .platform_utils import detect_platform, marker_matches, subprocess_creation_flags, uv_executable_path, venv_python_path
from .speech_engine import (
    MODELS, SpeechSettings, complete_model, cuda_libraries, download_speech_model,
    find_executable, find_model, gpu_info, mlx_whisper_available, model_cache_root,
    python_cuda, subtitle_edit_engine,
)


MANIFEST_PATH = APP_ROOT / "scripts" / "dependencies.json"
REQUIREMENTS_PATH = APP_ROOT / "requirements.txt"
DEPENDENCY_STATE_PATH = DATA_ROOT / "dependency_state.json"

CATEGORY_ORDER: tuple[str, ...] = (
    "core_runtime",
    "media_tools",
    "youtube_downloader",
    "local_transcription",
    "face_tracking",
    "advanced_asd",
    "platform_acceleration",
)
CATEGORY_LABELS: dict[str, str] = {
    "core_runtime": "Core Application Runtime",
    "media_tools": "Media Processing Tools",
    "youtube_downloader": "YouTube Downloader",
    "local_transcription": "Local Transcription",
    "face_tracking": "Face Tracking",
    "advanced_asd": "Advanced Active Speaker Detection",
    "platform_acceleration": "Platform-Specific Acceleration",
    # Backward-compatible aliases
    "bootstrap": "Core Application Runtime",
    "runtime": "Core Application Runtime",
    "python_package": "Core Application Runtime",
    "media_tool": "Media Processing Tools",
    "speech": "Local Transcription",
    "whisper_model": "Local Transcription",
    "vision": "Face Tracking",
    "gpu": "Platform-Specific Acceleration",
    "fonts": "Media Processing Tools",
}

ALLOW_NEWER_PACKAGES = {"yt-dlp", "yt-dlp-ejs", "mlx-whisper"}
NON_STARTUP_PACKAGES = {"pytest"}
_REQ_RE = re.compile(r"^([A-Za-z0-9_.-]+)\s*(==|>=|~=)\s*([^\s;]+)$")
_PRERELEASE_RE = re.compile(r"(?:a|b|rc|dev|alpha|beta)\d*$", re.IGNORECASE)


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
    platforms: tuple[str, ...] = ("windows", "macos")
    version_policy: str = "pinned"


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
    available_version: str = ""
    compatibility: str = "Compatible"  # Compatible | Pinned | Outdated | Incompatible | Optional
    error_detail: str = ""
    can_update: bool = False
    can_repair: bool = False
    can_install: bool = False


@dataclass(frozen=True, slots=True)
class DownloadMetrics:
    downloaded_bytes: int
    total_bytes: int | None
    speed_bps: float
    eta_seconds: float | None


@dataclass(frozen=True, slots=True)
class PackageManagerInfo:
    kind: str                  # "uv" | "pip" | "none"
    executable: str
    python_executable: str
    has_pip: bool
    has_uv: bool
    summary: str


def version_tuple(raw: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", raw.split("+")[0].split("-")[0])
    return tuple(int(p) for p in parts) if parts else (0,)


def is_stable_release(version: str) -> bool:
    cleaned = version.strip()
    if not cleaned:
        return False
    return not bool(_PRERELEASE_RE.search(cleaned)) and "dev" not in cleaned.lower()


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


def load_dependency_state(path: Path | None = None) -> dict[str, Any]:
    target = path or (DATA_ROOT / "dependency_state.json")
    if not target.is_file():
        return {}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_dependency_state(state: dict[str, Any], path: Path | None = None) -> None:
    target = path or (DATA_ROOT / "dependency_state.json")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(target)
    except OSError:
        pass


def detect_package_manager(root: Path | None = None, python_exe: Path | None = None) -> PackageManagerInfo:
    """Detect the active package installer cleanly, supporting uv-without-pip and pip-managed venvs."""
    app_root = root or APP_ROOT
    py = python_exe or venv_python_path(app_root, windowed=False)
    if not py.is_file():
        py = Path(sys.executable)

    uv_bin: Path | None = None
    bundled_uv = uv_executable_path(app_root)
    if bundled_uv.is_file():
        uv_bin = bundled_uv
    else:
        for cand in (
            shutil.which("uv"),
            "/opt/homebrew/bin/uv",
            "/usr/local/bin/uv",
            str(Path.home() / ".local" / "bin" / "uv"),
        ):
            if cand and Path(cand).is_file():
                uv_bin = Path(cand)
                break

    has_pip = False
    if py.is_file():
        try:
            probe = subprocess.run(
                [str(py), "-m", "pip", "--version"],
                capture_output=True, text=True, timeout=6, stdin=subprocess.DEVNULL,
                creationflags=subprocess_creation_flags(),
            )
            has_pip = probe.returncode == 0
        except (OSError, subprocess.SubprocessError):
            has_pip = False

    if uv_bin is not None:
        return PackageManagerInfo(
            kind="uv",
            executable=str(uv_bin),
            python_executable=str(py),
            has_pip=has_pip,
            has_uv=True,
            summary=f"uv ({uv_bin.name})" + (" + pip" if has_pip else " (uv-managed venv without pip)"),
        )
    if has_pip:
        return PackageManagerInfo(
            kind="pip",
            executable=str(py),
            python_executable=str(py),
            has_pip=True,
            has_uv=False,
            summary=f"pip ({py.name} -m pip)",
        )
    return PackageManagerInfo(
        kind="none",
        executable="",
        python_executable=str(py),
        has_pip=False,
        has_uv=False,
        summary="No package manager found (install uv or pip)",
    )


def _run_package_install(
    specs: list[str],
    *,
    upgrade: bool = False,
    root: Path | None = None,
    python_exe: Path | None = None,
    timeout: int = 300,
) -> subprocess.CompletedProcess[str]:
    pm = detect_package_manager(root=root, python_exe=python_exe)
    if pm.kind == "uv":
        cmd = [pm.executable, "pip", "install", "--python", pm.python_executable]
        if upgrade:
            cmd.append("--upgrade")
        cmd.extend(specs)
    elif pm.kind == "pip":
        cmd = [pm.python_executable, "-m", "pip", "install"]
        if upgrade:
            cmd.append("--upgrade")
        cmd.extend(specs)
    else:
        raise RuntimeError(
            "Neither uv nor pip is available for this virtual environment. "
            "Run First-Time Setup or install uv to manage Python dependencies."
        )
    env = {
        **os.environ,
        "UV_PYTHON_INSTALL_DIR": str((root or APP_ROOT) / ".runtime"),
        "UV_CACHE_DIR": str((root or APP_ROOT) / ".uv-cache"),
        "PYTHONIOENCODING": "utf-8",
    }
    return subprocess.run(
        cmd, cwd=root or APP_ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
        timeout=timeout, creationflags=subprocess_creation_flags(), env=env,
    )


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
            platforms=tuple(item.get("platforms", ["windows", "macos"])),
            version_policy=str(item.get("version_policy", "pinned")),
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


def _requirement_specs(plat: str | None = None, arch: str | None = None) -> dict[str, tuple[str, str]]:
    """Return {pkg_name: (operator, version)} for requirements matching the target platform."""
    specs: dict[str, tuple[str, str]] = {}
    if not REQUIREMENTS_PATH.is_file():
        return specs
    for line in REQUIREMENTS_PATH.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        req, _, marker = stripped.partition(";")
        if not marker_matches(marker, plat=plat, arch=arch):
            continue
        match = _REQ_RE.match(req.strip())
        if match:
            specs[match.group(1)] = (match.group(2), match.group(3))
    return specs


def _pinned_requirements(plat: str | None = None, arch: str | None = None) -> dict[str, str]:
    return {name: ver for name, (_op, ver) in _requirement_specs(plat=plat, arch=arch).items()}


def _version_compatible(name: str, op: str, expected: str, installed: str) -> bool:
    inst_t = version_tuple(installed)
    exp_t = version_tuple(expected)
    if op == ">=" or name.lower() in ALLOW_NEWER_PACKAGES:
        return inst_t >= exp_t
    if op == "~=":
        return inst_t >= exp_t and inst_t[:1] == exp_t[:1]
    return installed == expected or inst_t == exp_t


def _venv_site_packages(root: Path) -> Path:
    if os.name == "nt":
        return root / ".venv" / "Lib" / "site-packages"
    lib_dir = root / ".venv" / "lib"
    if lib_dir.is_dir():
        found = next(lib_dir.glob("python*/site-packages"), None)
        if found is not None:
            return found
    return lib_dir / "python3.12" / "site-packages"


def check_yt_dlp_update(*, timeout: int = 10, state_path: Path | None = None) -> dict[str, Any]:
    """Check PyPI official stable releases for yt-dlp without installing anything."""
    try:
        installed = importlib.metadata.version("yt-dlp")
    except importlib.metadata.PackageNotFoundError:
        installed = ""
    req = urllib.request.Request(
        "https://pypi.org/pypi/yt-dlp/json",
        headers={"User-Agent": "AutoClipStudio/1.0", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        latest = str(payload.get("info", {}).get("version") or "").strip()
        if not is_stable_release(latest):
            # Pick highest stable release key if info.version is somehow a pre-release.
            releases = [v for v in (payload.get("releases") or {}).keys() if is_stable_release(str(v))]
            latest = max(releases, key=version_tuple) if releases else ""
        update_avail = bool(latest and (not installed or version_tuple(latest) > version_tuple(installed)))
        state = load_dependency_state(state_path)
        state["yt_dlp_latest_checked"] = latest
        state["yt_dlp_checked_at"] = int(time.time())
        save_dependency_state(state, state_path)
        return {
            "installed": installed or "Not installed",
            "latest": latest,
            "update_available": update_avail,
            "error": "",
        }
    except Exception as exc:
        return {
            "installed": installed or "Not installed",
            "latest": "",
            "update_available": False,
            "error": redact(str(exc)),
        }


def validate_yt_dlp(python_exe: Path | None = None) -> tuple[bool, str]:
    """Verify yt-dlp CLI, version output, and YouTube extractor import cleanly."""
    py = python_exe or venv_python_path(APP_ROOT, windowed=False)
    if not py.is_file():
        py = Path(sys.executable)
    try:
        ver_res = subprocess.run(
            [str(py), "-m", "yt_dlp", "--version"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=15, stdin=subprocess.DEVNULL, creationflags=subprocess_creation_flags(),
        )
        if ver_res.returncode != 0:
            return False, (ver_res.stderr or ver_res.stdout or "yt-dlp --version failed").strip()
        version = (ver_res.stdout or "").strip().splitlines()[-1]
        ext_res = subprocess.run(
            [str(py), "-c", "from yt_dlp.extractor.youtube import YoutubeIE; print(YoutubeIE.IE_NAME)"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=15, stdin=subprocess.DEVNULL, creationflags=subprocess_creation_flags(),
        )
        if ext_res.returncode != 0:
            return False, (ext_res.stderr or ext_res.stdout or "YouTube extractor import failed").strip()
        return True, version
    except Exception as exc:
        return False, redact(str(exc))


def update_yt_dlp(
    *,
    target_version: str | None = None,
    is_job_active: Callable[[], bool] | None = None,
    root: Path | None = None,
    python_exe: Path | None = None,
    state_path: Path | None = None,
) -> dict[str, Any]:
    """Update yt-dlp to the latest stable release via uv or pip, with post-update validation and rollback."""
    if is_job_active is not None and is_job_active():
        raise RuntimeError("Cannot update yt-dlp while a processing job is active. Wait for the job to finish or cancel it first.")
    if target_version and not is_stable_release(target_version):
        raise ValueError(f"Refusing non-stable yt-dlp version '{target_version}'. Only stable releases are supported.")

    py = python_exe or venv_python_path(root or APP_ROOT, windowed=False)
    if not py.is_file():
        py = Path(sys.executable)

    prev_ok, prev_ver = validate_yt_dlp(py)
    previous_version = prev_ver if prev_ok else ""
    spec = f"yt-dlp=={target_version}" if target_version else "yt-dlp"
    res = _run_package_install([spec], upgrade=(target_version is None), root=root, python_exe=py, timeout=300)
    if res.returncode != 0:
        detail = redact((res.stderr or res.stdout or f"Exited with code {res.returncode}").strip())
        raise RuntimeError(f"yt-dlp update failed:\n{detail}")

    ok, new_ver_or_err = validate_yt_dlp(py)
    if not ok:
        rollback_note = ""
        if previous_version:
            rb = _run_package_install([f"yt-dlp=={previous_version}"], upgrade=False, root=root, python_exe=py, timeout=300)
            rollback_note = (
                f" Rolled back to yt-dlp {previous_version}."
                if rb.returncode == 0
                else f" Rollback to {previous_version} also failed."
            )
        raise RuntimeError(f"Updated yt-dlp failed compatibility validation: {new_ver_or_err}.{rollback_note}")

    state = load_dependency_state(state_path)
    state["yt_dlp_version"] = new_ver_or_err
    if previous_version and previous_version != new_ver_or_err:
        state["yt_dlp_previous_version"] = previous_version
    state["yt_dlp_updated_at"] = int(time.time())
    state["yt_dlp_latest_checked"] = new_ver_or_err
    save_dependency_state(state, state_path)
    return {
        "previous_version": previous_version,
        "installed_version": new_ver_or_err,
        "rolled_back": False,
    }


def install_yunet_asd_model(
    *,
    cancel_event: threading.Event | None = None,
    on_progress: Callable[[DownloadMetrics], None] | None = None,
) -> Path:
    """Download and verify the optional 227 KB MIT OpenCV YuNet 5-point facial landmark model."""
    from .smart_reframe import YUNET_FILENAME, YUNET_URL

    target = MODEL_CACHE / YUNET_FILENAME
    download_resumable(YUNET_URL, target, cancel_event=cancel_event, on_progress=on_progress)
    try:
        import cv2
        det = cv2.FaceDetectorYN.create(str(target), "", (320, 320))
        if det is None:
            raise RuntimeError("OpenCV could not initialize FaceDetectorYN with the downloaded model.")
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return target


def repair_component(
    component_id: str,
    *,
    is_job_active: Callable[[], bool] | None = None,
    cloud_only: bool = False,
) -> str:
    """Selectively repair or install a single managed component without clobbering other versions."""
    if component_id == "yt_dlp":
        info = update_yt_dlp(is_job_active=is_job_active)
        return f"yt-dlp {info['installed_version']} verified and ready."
    if component_id == "yunet_asd_model":
        path = install_yunet_asd_model()
        return f"YuNet 5-point landmark model installed ({format_bytes(path.stat().st_size)})."
    if component_id == "whisper_model_large_v3_turbo":
        if cloud_only:
            return "Skipped local Whisper model download because Cloud-only mode is enabled."
        path = download_whisper_model_checked("large-v3-turbo")
        return f"Whisper large-v3-turbo ready at {path}"

    setup_py = APP_ROOT / "scripts" / "setup.py"
    if not setup_py.is_file():
        raise FileNotFoundError("scripts/setup.py was not found.")
    args = [sys.executable, str(setup_py), "--repair-only", "--no-tests"]
    if cloud_only:
        args.append("--cloud-only")
    res = subprocess.run(
        args, cwd=APP_ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=600, stdin=subprocess.DEVNULL, creationflags=subprocess_creation_flags(),
    )
    if res.returncode != 0:
        err = redact((res.stderr or res.stdout or f"Exited with code {res.returncode}").strip())
        raise RuntimeError(err)
    return f"Component '{component_id}' repaired and verified."


def inspect_all_dependencies(
    manifest_path: Path = MANIFEST_PATH,
    *,
    speech_settings: SpeechSettings | None = None,
    cloud_only: bool = False,
    target_system: str | None = None,
    available_versions: dict[str, str] | None = None,
) -> list[DependencyStatus]:
    """Inspect all components in the manifest for the current platform, reusing existing installations safely."""
    specs, _, default_model = load_manifest(manifest_path)
    pinfo = detect_platform()
    active_system = target_system or pinfo.system
    speech_settings = speech_settings or SpeechSettings()
    plat_tag = "win32" if active_system == "windows" else ("darwin" if active_system == "macos" else sys.platform)
    arch_tag = "arm64" if active_system == "macos" else pinfo.machine
    req_specs = _requirement_specs(plat=plat_tag, arch=arch_tag)
    pins = {k: v[1] for k, v in req_specs.items()}
    gpu = gpu_info() if active_system == "windows" else None
    has_gpu = gpu is not None
    state_cache = load_dependency_state()
    avail_map = dict(available_versions or {})
    if "yt_dlp" not in avail_map and state_cache.get("yt_dlp_latest_checked"):
        avail_map["yt_dlp"] = str(state_cache["yt_dlp_latest_checked"])
    statuses: list[DependencyStatus] = []

    for spec in specs:
        if active_system not in spec.platforms:
            continue
        if spec.id == "uv":
            uv_path = uv_executable_path(APP_ROOT)
            if not uv_path.is_file():
                which_uv = shutil.which("uv")
                if which_uv:
                    statuses.append(DependencyStatus(
                        spec, "Reused existing", spec.version, which_uv, 0, False,
                        "System uv detected", True, compatibility="Compatible",
                    ))
                    continue
                statuses.append(DependencyStatus(
                    spec, "Missing", "—", str(uv_path), 0, False,
                    f"{uv_path.name} not installed", compatibility="Incompatible", can_repair=True,
                ))
            else:
                try:
                    out = subprocess.run(
                        [str(uv_path), "--version"], capture_output=True, text=True, timeout=5,
                        creationflags=subprocess_creation_flags(),
                    ).stdout.strip()
                    if out.startswith("uv "):
                        uv_ver = out.split()[1]
                        outdated = version_tuple(uv_ver) < version_tuple(spec.version)
                        statuses.append(DependencyStatus(
                            spec, "Ready", uv_ver, str(uv_path), uv_path.stat().st_size, outdated,
                            "uv package manager verified",
                            compatibility="Outdated" if outdated else "Compatible",
                            can_update=outdated,
                        ))
                    else:
                        statuses.append(DependencyStatus(
                            spec, "Corrupted", "unknown", str(uv_path), uv_path.stat().st_size, False,
                            f"{uv_path.name} failed version check", compatibility="Incompatible", can_repair=True,
                        ))
                except (OSError, subprocess.SubprocessError, IndexError) as exc:
                    statuses.append(DependencyStatus(
                        spec, "Corrupted", "unknown", str(uv_path), uv_path.stat().st_size, False,
                        f"{uv_path.name} could not run", compatibility="Incompatible", error_detail=str(exc), can_repair=True,
                    ))

        elif spec.id == "python_runtime":
            ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
            py_exe = venv_python_path(APP_ROOT, windowed=False)
            pyw_exe = venv_python_path(APP_ROOT, windowed=True)
            rt_dir = APP_ROOT / ".runtime"
            if (3, 11) <= sys.version_info[:2] < (3, 13) and (py_exe.is_file() or pyw_exe.is_file()):
                reused = not rt_dir.exists()
                statuses.append(DependencyStatus(
                    spec, "Reused existing" if reused else "Ready", ver,
                    sys.executable, directory_size(rt_dir), False,
                    f"Compatible Python ({pinfo.machine}) runtime active", reused,
                    compatibility="Compatible",
                ))
            else:
                statuses.append(DependencyStatus(
                    spec, "Missing", ver, str(rt_dir), 0, False,
                    "Python 3.11/3.12 runtime required", compatibility="Incompatible", can_repair=True,
                ))

        elif spec.id == "python_venv":
            venv = APP_ROOT / ".venv"
            pyw = venv_python_path(APP_ROOT, windowed=True)
            if venv.is_dir() and pyw.is_file():
                pm = detect_package_manager(APP_ROOT)
                statuses.append(DependencyStatus(
                    spec, "Ready", spec.version, str(venv), directory_size(venv), False,
                    f"Project virtual environment active • Installer: {pm.summary}",
                    compatibility="Compatible",
                ))
            else:
                statuses.append(DependencyStatus(
                    spec, "Missing", "—", str(venv), 0, False,
                    "Virtual environment missing", compatibility="Incompatible", can_repair=True,
                ))

        elif spec.id == "ffmpeg":
            ff_dir = APP_ROOT / "tools" / "ffmpeg"
            ff_exe = bundled_binary("ffmpeg", root=APP_ROOT)
            fp_exe = bundled_binary("ffprobe", root=APP_ROOT)
            if ff_exe and fp_exe and ff_exe.is_file() and fp_exe.is_file():
                try:
                    out = subprocess.run([str(ff_exe), "-version"], capture_output=True, text=True, timeout=8,
                                         creationflags=subprocess_creation_flags()).stdout
                    ver_line = out.splitlines()[0].split()[2] if out.startswith("ffmpeg version ") else "unknown"
                    reused_ext = not ff_exe.resolve().is_relative_to(ff_dir.resolve()) if ff_dir.exists() else True
                    state = "Reused existing" if reused_ext else "Ready"
                    statuses.append(DependencyStatus(
                        spec, state, ver_line, str(ff_exe.parent), directory_size(ff_dir), False,
                        "FFmpeg & FFprobe verified", reused_ext, compatibility="Compatible",
                    ))
                except (OSError, subprocess.SubprocessError, IndexError) as exc:
                    statuses.append(DependencyStatus(
                        spec, "Corrupted", "—", str(ff_dir), directory_size(ff_dir), False,
                        "FFmpeg failed executable check", compatibility="Incompatible", error_detail=str(exc), can_repair=True,
                    ))
            else:
                sys_ff = shutil.which("ffmpeg")
                sys_fp = shutil.which("ffprobe")
                if sys_ff and sys_fp:
                    statuses.append(DependencyStatus(
                        spec, "Reused existing", "system", str(Path(sys_ff).parent), 0, False,
                        "Compatible system FFmpeg detected", True, compatibility="Compatible",
                    ))
                else:
                    statuses.append(DependencyStatus(
                        spec, "Missing", "—", str(ff_dir), 0, False,
                        "FFmpeg & FFprobe missing", compatibility="Incompatible", can_repair=True,
                    ))

        elif spec.id == "mlx_whisper":
            if active_system == "macos":
                try:
                    inst = importlib.metadata.version("mlx-whisper")
                    p = _venv_site_packages(APP_ROOT) / "mlx_whisper"
                    statuses.append(DependencyStatus(
                        spec, "Ready", inst, str(p), directory_size(p), False,
                        "MLX Whisper Apple Silicon acceleration ready", compatibility="Compatible",
                    ))
                except importlib.metadata.PackageNotFoundError:
                    statuses.append(DependencyStatus(
                        spec, "Missing" if pinfo.is_apple_silicon else "Optional", "—", spec.install_path, 0, False,
                        "Install mlx-whisper for Apple Silicon acceleration",
                        compatibility="Incompatible" if pinfo.is_apple_silicon else "Optional",
                        can_repair=pinfo.is_apple_silicon,
                    ))
            continue

        elif spec.id == "pyside6":
            expected = pins.get("PySide6", spec.version)
            try:
                inst = importlib.metadata.version("PySide6")
                matched = inst == expected
                p = _venv_site_packages(APP_ROOT) / "PySide6"
                statuses.append(DependencyStatus(
                    spec, "Ready", inst, str(p), directory_size(p), not matched,
                    f"Qt GUI runtime {inst} (pinned to {expected} for UI stability)",
                    compatibility="Pinned" if matched else "Incompatible",
                    can_repair=not matched,
                ))
            except importlib.metadata.PackageNotFoundError:
                statuses.append(DependencyStatus(
                    spec, "Missing", "—", spec.install_path, 0, False,
                    "PySide6 not installed", compatibility="Incompatible", can_repair=True,
                ))

        elif spec.id == "python_packages":
            missing, mismatched = [], []
            checked_count = 0
            for pkg, (op, exp) in req_specs.items():
                if pkg.lower() in NON_STARTUP_PACKAGES:
                    continue
                checked_count += 1
                try:
                    inst = importlib.metadata.version(pkg)
                    if not _version_compatible(pkg, op, exp, inst):
                        mismatched.append(f"{pkg}=={inst} (need {op}{exp})")
                except importlib.metadata.PackageNotFoundError:
                    missing.append(pkg)
            site_dir = _venv_site_packages(APP_ROOT)
            if missing:
                statuses.append(DependencyStatus(
                    spec, "Missing", f"{checked_count - len(missing)}/{checked_count}", str(site_dir),
                    directory_size(site_dir), False, "Missing: " + ", ".join(missing),
                    compatibility="Incompatible", can_repair=True,
                ))
            elif mismatched:
                statuses.append(DependencyStatus(
                    spec, "Ready", f"{checked_count}/{checked_count}", str(site_dir),
                    directory_size(site_dir), True, "Version diff: " + ", ".join(mismatched),
                    compatibility="Outdated", can_repair=True,
                ))
            else:
                statuses.append(DependencyStatus(
                    spec, "Ready", f"{checked_count}/{checked_count} verified", str(site_dir),
                    directory_size(site_dir), False, "All required Python packages compatible",
                    compatibility="Compatible",
                ))

        elif spec.id == "yt_dlp":
            expected = pins.get("yt-dlp", spec.version)
            avail = avail_map.get("yt_dlp", "")
            pkg_exe = APP_ROOT / "tools" / "package" / "yt-dlp.exe"
            try:
                inst = importlib.metadata.version("yt-dlp")
                below_floor = version_tuple(inst) < version_tuple(expected)
                has_newer = bool(avail and version_tuple(avail) > version_tuple(inst))
                update_avail = below_floor or has_newer
                compat = "Outdated" if below_floor else "Compatible"
                detail = f"yt-dlp {inst} active (minimum >={expected}; user updates preserved)"
                if has_newer:
                    detail = f"yt-dlp {inst} active — stable update {avail} available on PyPI"
                elif below_floor:
                    detail = f"yt-dlp {inst} is outdated (minimum >={expected} required for YouTube)"
                statuses.append(DependencyStatus(
                    spec, "Ready", inst, str(_venv_site_packages(APP_ROOT) / "yt_dlp"), 0,
                    update_avail, detail,
                    available_version=avail,
                    compatibility=compat,
                    can_update=True,
                    can_repair=below_floor,
                ))
            except importlib.metadata.PackageNotFoundError:
                if pkg_exe.is_file():
                    statuses.append(DependencyStatus(
                        spec, "Reused existing", "bundled", str(pkg_exe), pkg_exe.stat().st_size,
                        False, "Bundled yt-dlp.exe", True, available_version=avail, compatibility="Compatible",
                    ))
                else:
                    statuses.append(DependencyStatus(
                        spec, "Missing", "—", spec.install_path, 0, True, "yt-dlp missing",
                        available_version=avail, compatibility="Incompatible", can_repair=True,
                    ))

        elif spec.id == "opencv_face_model":
            try:
                import cv2
                inst = importlib.metadata.version("opencv-python-headless")
                cascade = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
                if cascade.is_file():
                    statuses.append(DependencyStatus(
                        spec, "Ready", inst, str(cascade), cascade.stat().st_size, False,
                        "OpenCV + multi-orientation Haar cascade ready", compatibility="Pinned",
                    ))
                else:
                    statuses.append(DependencyStatus(
                        spec, "Corrupted", inst, str(cascade), 0, False,
                        "Haar cascade XML missing", compatibility="Incompatible", can_repair=True,
                    ))
            except Exception as exc:
                statuses.append(DependencyStatus(
                    spec, "Missing", "—", spec.install_path, 0, False,
                    "OpenCV missing", compatibility="Incompatible", error_detail=str(exc), can_repair=True,
                ))

        elif spec.id == "yunet_asd_model":
            from .smart_reframe import find_yunet_model
            yunet = find_yunet_model()
            if yunet is not None:
                statuses.append(DependencyStatus(
                    spec, "Ready", spec.version, str(yunet), yunet.stat().st_size, False,
                    "YuNet 5-point landmark detector active (~5x faster & sharper multi-person ASD)",
                    compatibility="Compatible",
                ))
            else:
                statuses.append(DependencyStatus(
                    spec, "Optional", "Not installed", str(MODEL_CACHE), 0, False,
                    "Optional 227 KB MIT landmark upgrade; built-in multi-orientation Haar fallback active",
                    compatibility="Optional", can_install=True,
                ))

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
                    compatibility="Compatible",
                ))
            elif custom_exe and custom_exe.is_file():
                statuses.append(DependencyStatus(
                    spec, "Ready", "Purfview XXL", str(custom_exe), custom_exe.stat().st_size, False,
                    "Standalone Purfview XXL ready", compatibility="Compatible",
                ))
            elif fw_ver:
                statuses.append(DependencyStatus(
                    spec, "Ready", f"faster-whisper {fw_ver}", str(_venv_site_packages(APP_ROOT) / "faster_whisper"), 0, False,
                    "Python Faster-Whisper ready", compatibility="Pinned",
                ))
            else:
                statuses.append(DependencyStatus(
                    spec, "Missing", "—", spec.install_path, 0, False,
                    "No local transcription engine", compatibility="Incompatible", can_repair=True,
                ))

        elif spec.id == "cuda_runtime":
            sub_exe = find_executable(speech_settings.executable)
            xxl_libs = cuda_libraries(sub_exe) if sub_exe else ()
            py_cuda = python_cuda()
            if not has_gpu:
                statuses.append(DependencyStatus(
                    spec, "Optional", "CPU mode", "—", 0, False,
                    "No NVIDIA GPU detected; CUDA not required", compatibility="Optional",
                ))
            elif py_cuda or xxl_libs:
                detail = "Python CUDA ready" if py_cuda else f"Reused Purfview CUDA libs ({len(xxl_libs)} DLLs)"
                statuses.append(DependencyStatus(
                    spec, "Reused existing" if xxl_libs and not py_cuda else "Ready",
                    "CUDA 12 / cuDNN 9", str(sub_exe.parent if sub_exe else ""), 0, False,
                    detail, bool(xxl_libs and not py_cuda), compatibility="Compatible",
                ))
            else:
                statuses.append(DependencyStatus(
                    spec, "Optional", "Unavailable", "—", 0, False,
                    "NVIDIA GPU present; CPU fallback active unless CUDA DLLs or Purfview XXL installed",
                    compatibility="Optional",
                ))

        elif spec.id == "whisper_model_large_v3_turbo":
            model_path = find_model(default_model, speech_settings, find_executable(speech_settings.executable))
            if model_path and complete_model(model_path):
                is_external = not model_path.resolve().is_relative_to(MODEL_CACHE.resolve()) if MODEL_CACHE.exists() else True
                statuses.append(DependencyStatus(
                    spec, "Reused existing" if is_external else "Ready", default_model,
                    str(model_path), directory_size(model_path), False,
                    "Reused external Whisper cache" if is_external else "Cached locally", is_external,
                    compatibility="Compatible",
                ))
            elif cloud_only:
                statuses.append(DependencyStatus(
                    spec, "Optional", "Not needed (Cloud)", str(MODEL_CACHE), 0, False,
                    "Skipped for cloud-only transcription", compatibility="Optional",
                ))
            else:
                statuses.append(DependencyStatus(
                    spec, "Optional", "Not downloaded", str(MODEL_CACHE), 0, False,
                    "Recommended default model; download below when using local STT",
                    compatibility="Optional", can_install=True,
                ))

        elif spec.id == "caption_fonts":
            installed = _installed_fonts()
            needed = sorted({t.font for t in TEMPLATES.values()})
            present = [f for f in needed if f.lower() in installed]
            missing_fonts = [f for f in needed if f.lower() not in installed]
            detail = f"{len(present)}/{len(needed)} template fonts available"
            if missing_fonts:
                detail += f" (fallback used for: {', '.join(missing_fonts)})"
            font_path = "/System/Library/Fonts" if active_system == "macos" else spec.install_path
            statuses.append(DependencyStatus(
                spec, "Ready", f"{len(present)}/{len(needed)}", font_path, 0, False,
                detail, compatibility="Compatible",
            ))

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
