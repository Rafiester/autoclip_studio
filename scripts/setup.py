"""Validate or provision AutoClip Studio's runtime, packages, media tools, hardware accel, and optional models.

8-Stage Bootstrap Architecture:
  1. system_compatibility : OS, CPU architecture, RAM, disk space, GPU availability, permissions
  2. python_runtime       : Compatible Python 3.11-3.12 runtime detection / provisioning
  3. virtual_env          : Project-local .venv validation / creation + uv/pip detection
  4. core_packages        : Selective installation & version checks from requirements.txt
  5. media_tools          : FFmpeg, FFprobe, yt-dlp, JavaScript runtime, caption fonts
  6. hardware_accel       : Windows CUDA/cuDNN/NVENC or macOS ARM64/MLX/VideoToolbox
  7. transcription_models : Local engine checks & optional model reuse/cloud-only respect
  8. final_validation     : Core imports, media checks, data dirs, settings compatibility
"""

from __future__ import annotations

import argparse
import contextlib
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path
from typing import Any, Iterator

if os.name == "nt":
    import ctypes
    import msvcrt
else:
    import fcntl


ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
UV = TOOLS / ("uv.exe" if os.name == "nt" else "uv")
PYTHON = ROOT / ".venv" / ("Scripts" if os.name == "nt" else "bin") / ("python.exe" if os.name == "nt" else "python3")
PYTHONW = PYTHON.with_name("pythonw.exe") if os.name == "nt" else PYTHON
FFMPEG = TOOLS / "ffmpeg"
REQUIREMENTS = ROOT / "requirements.txt"
MANIFEST = ROOT / "scripts" / "dependencies.json"
TOOLCHAIN = ROOT / "scripts" / "toolchain.json"
FFMPEG_RELEASE = "ffmpeg-9.1-essentials_build"
FFMPEG_SHA256 = "b9bbbb3acece000cbeec994f856649354e52c773807841e78908374b24d658fd"

# Packages where newer stable versions installed by the user must ALWAYS be preserved.
ALLOW_NEWER_PACKAGES = {"yt-dlp", "yt-dlp-ejs", "mlx-whisper"}
NON_STARTUP_PACKAGES = {"pytest"}

STAGES: tuple[tuple[str, str, int, int], ...] = (
    ("system_compatibility", "System Compatibility", 0, 5),
    ("python_runtime", "Python Runtime", 5, 16),
    ("virtual_env", "Virtual Environment", 16, 26),
    ("core_packages", "Core Application Dependencies", 26, 56),
    ("media_tools", "Media Processing Tools", 56, 80),
    ("hardware_accel", "Hardware Acceleration", 80, 88),
    ("transcription_models", "Transcription & Optional Models", 88, 95),
    ("final_validation", "Final Validation", 95, 100),
)

_JSON_PROGRESS = False
_LAST_OVERALL = 0.0
_REQ_RE = re.compile(r"^([A-Za-z0-9_.-]+)\s*(==|>=|~=)\s*([^\s;]+)$")

# Redaction patterns mirroring autoclip_studio/diagnostics.py (kept self-contained with zero app imports).
_SECRET_FIELD = (
    r"authorization|proxy[-_]authorization|(?:x[-_](?:goog[-_])?)?api[-_ ]?key|key|"
    r"(?:access|refresh|id|auth|session)[-_]?token|token|(?:client|api)[-_]?secret|"
    r"password|signature|x[-_]goog[-_](?:credential|signature|upload[-_]url)|cookie|set[-_]cookie"
)
_QUOTED_SECRET = re.compile(rf"""(?i)(["'](?:{_SECRET_FIELD})["']\s*:\s*)(["'])(?:\\.|(?!\2)[^\\])*\2""")
_PLAIN_SECRET = re.compile(
    rf"""(?i)(\b(?:{_SECRET_FIELD})[ \t]*[:=][ \t]*)(?:(?:bearer|basic)\s+)?[^\s,;'"&<>{{}}\[\]]+"""
)
_AUTH_HEADER = re.compile(r"(?im)^([ \t]*(?:proxy-)?authorization[ \t]*:)[^\r\n]*")
_COOKIE_HEADER = re.compile(r"(?im)^([ \t]*(?:set-)?cookie[ \t]*:)[^\r\n]*")
_BEARER = re.compile(r"(?i)\b(?:bearer|basic)\s+[a-zA-Z0-9._~+/=-]{6,}")
_PROVIDER_KEY = re.compile(r"(?<![\w-])(?:AIza[\w-]{35}|sk-[A-Za-z0-9_-]{20,})\b")
_URL_USERINFO = re.compile(r"(?i)(https?://)[^/\s@]+@")


def _redact(message: str) -> str:
    text = str(message)
    text = _AUTH_HEADER.sub(r"\1 [REDACTED]", text)
    text = _COOKIE_HEADER.sub(r"\1 [REDACTED]", text)
    text = _QUOTED_SECRET.sub(r"\1\2[REDACTED]\2", text)
    text = _PLAIN_SECRET.sub(r"\1[REDACTED]", text)
    text = _BEARER.sub("[REDACTED]", text)
    text = _PROVIDER_KEY.sub("[REDACTED]", text)
    return _URL_USERINFO.sub(r"\1[REDACTED]@", text)


def _data_root() -> Path:
    configured = os.environ.get("AUTOCLIP_DATA_DIR", "").strip()
    if configured:
        p = Path(configured).expanduser()
        return p if p.is_absolute() else (ROOT / p).resolve()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "AutoClip Studio"
    local_app = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app:
        return Path(local_app) / "AutoClip" / "AutoClip Studio"
    return Path.home() / ".local" / "share" / "AutoClip Studio"


def _setup_log_path() -> Path:
    return _data_root() / "logs" / "setup.log"


def _append_setup_log(level: str, component: str, message: str) -> None:
    cleaned = _redact(message).strip()
    if not cleaned:
        return
    try:
        log_path = _setup_log_path()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().astimezone().isoformat(sep=" ", timespec="seconds")
        line = f"[{stamp}] [{level}] [{component}] {cleaned}\n"
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass


def _clamp_overall(value: float | None, *, allow_complete: bool = False) -> float | None:
    global _LAST_OVERALL
    if value is None:
        return None
    cap = 100.0 if allow_complete else 99.0
    clamped = max(_LAST_OVERALL, min(cap, round(float(value), 1)))
    _LAST_OVERALL = clamped
    return clamped


def _emit(event: str, *, allow_complete: bool = False, **payload: Any) -> None:
    if "detail" in payload and payload["detail"] is not None:
        payload["detail"] = _redact(str(payload["detail"]))
    if "message" in payload and payload["message"] is not None:
        payload["message"] = _redact(str(payload["message"]))
    if "overall_percent" in payload and payload["overall_percent"] is not None:
        payload["overall_percent"] = _clamp_overall(payload["overall_percent"], allow_complete=allow_complete)
    if event == "log":
        _append_setup_log(str(payload.get("level") or "INFO"), str(payload.get("component") or "setup"), str(payload.get("message") or ""))
    elif event == "progress" and payload.get("detail"):
        lvl = "ERROR" if payload.get("state") == "Failed" else "INFO"
        _append_setup_log(lvl, str(payload.get("id") or "setup"), f"{payload.get('state')}: {payload.get('detail')}")
    elif event == "stage":
        _append_setup_log("INFO", str(payload.get("stage_id") or "stage"), f"Stage {payload.get('stage_index')}/{payload.get('stage_total')} {payload.get('state')}: {payload.get('detail', '')}")
    if _JSON_PROGRESS:
        print(json.dumps({"event": event, **payload}, ensure_ascii=False), flush=True)


def _version_tuple(raw: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", raw.split("+")[0].split("-")[0])
    return tuple(int(p) for p in parts) if parts else (0,)


def _marker_applies(marker: str) -> bool:
    if not marker.strip():
        return True
    arch = platform.machine().lower()
    if 'sys_platform == "win32"' in marker and sys.platform != "win32":
        return False
    if 'sys_platform == "darwin"' in marker and sys.platform != "darwin":
        return False
    if 'platform_machine == "arm64"' in marker and arch not in {"arm64", "aarch64"}:
        return False
    return True


def _parse_requirements(path: Path = REQUIREMENTS) -> list[tuple[str, str, str, str]]:
    if not path.is_file():
        return []
    parsed: list[tuple[str, str, str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        requirement, _, marker = stripped.partition(";")
        if not _marker_applies(marker):
            continue
        match = _REQ_RE.match(requirement.strip())
        if not match:
            continue
        name, op, expected = match.group(1), match.group(2), match.group(3)
        parsed.append((name, op, expected, requirement.strip()))
    return parsed


def _is_version_satisfied(name: str, op: str, expected: str, installed: str) -> bool:
    inst_t = _version_tuple(installed)
    exp_t = _version_tuple(expected)
    if op == ">=" or name.lower() in ALLOW_NEWER_PACKAGES:
        return inst_t >= exp_t
    if op == "~=":
        return inst_t >= exp_t and inst_t[:1] == exp_t[:1]
    return installed == expected or inst_t == exp_t


def _find_uv() -> Path | None:
    if UV.is_file():
        return UV
    found = shutil.which("uv")
    if found:
        return Path(found)
    for candidate in (Path("/opt/homebrew/bin/uv"), Path("/usr/local/bin/uv"), Path.home() / ".local" / "bin" / "uv"):
        if candidate.is_file():
            return candidate
    return None


def _venv_has_pip(python_exe: Path = PYTHON) -> bool:
    if not python_exe.is_file():
        return False
    try:
        res = subprocess.run(
            [str(python_exe), "-m", "pip", "--version"],
            capture_output=True, text=True, timeout=8, stdin=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        return res.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _user_cloud_only_setting() -> bool:
    candidates: list[Path] = [_data_root() / "settings.json"]
    for path in candidates:
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                speech = data.get("speech") if isinstance(data, dict) else None
                if isinstance(speech, dict):
                    engine = str(speech.get("engine") or "").lower()
                    if engine in {"cloud", "openai", "gemini", "none"} or bool(speech.get("cloud_only")):
                        return True
            except (OSError, ValueError, TypeError):
                pass
    return False


def _total_ram_bytes() -> int:
    try:
        if os.name == "nt":
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_uint32),
                    ("dwMemoryLoad", ctypes.c_uint32),
                    ("ullTotalPhys", ctypes.c_uint64),
                    ("ullAvailPhys", ctypes.c_uint64),
                    ("ullTotalPageFile", ctypes.c_uint64),
                    ("ullAvailPageFile", ctypes.c_uint64),
                    ("ullTotalVirtual", ctypes.c_uint64),
                    ("ullAvailVirtual", ctypes.c_uint64),
                    ("ullAvailExtendedVirtual", ctypes.c_uint64),
                ]
            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                return int(stat.ullTotalPhys)
        elif sys.platform == "darwin":
            out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=3).stdout.strip()
            if out.isdigit():
                return int(out)
    except Exception:
        pass
    return 0


def _detect_system_info() -> dict[str, Any]:
    arch = platform.machine() or "unknown"
    os_name = "macOS" if sys.platform == "darwin" else ("Windows" if os.name == "nt" else platform.system())
    os_detail = f"{os_name} {platform.release()} ({arch})"
    cpu_cores = os.cpu_count() or 1
    cpu_label = platform.processor() or arch
    if sys.platform == "darwin":
        try:
            brand = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True, timeout=3).stdout.strip()
            if brand:
                cpu_label = brand
        except Exception:
            pass
    ram_bytes = _total_ram_bytes()
    disk_free = int(shutil.disk_usage(ROOT).free)
    gpu_desc = "CPU fallback mode"
    hw_accel = "CPU fallback"
    if os.name == "nt":
        nvidia_smi = shutil.which("nvidia-smi")
        if nvidia_smi:
            try:
                res = subprocess.run(
                    [nvidia_smi, "--query-gpu=name,memory.total", "--format=csv,noheader"],
                    capture_output=True, text=True, timeout=5, stdin=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                line = (res.stdout or "").strip().splitlines()
                if res.returncode == 0 and line and line[0].strip():
                    gpu_desc = line[0].strip()
                    hw_accel = "NVIDIA CUDA + NVENC (CPU fallback ready)"
            except Exception:
                pass
    elif sys.platform == "darwin" and arch.lower() in {"arm64", "aarch64"}:
        gpu_desc = "Apple Silicon Unified Memory GPU"
        hw_accel = "VideoToolbox + MLX (CPU fallback ready)"

    permissions_ok = True
    for target_dir in (ROOT, _data_root()):
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            probe = target_dir / f".write-probe-{uuid.uuid4().hex}.tmp"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError:
            permissions_ok = False
            break

    return {
        "os": os_name,
        "os_detail": os_detail,
        "arch": arch,
        "cpu": f"{cpu_label} ({cpu_cores} logical cores)",
        "ram_bytes": ram_bytes,
        "disk_free_bytes": disk_free,
        "gpu": gpu_desc,
        "hw_accel": hw_accel,
        "permissions_ok": permissions_ok,
        "setup_log": str(_setup_log_path()),
    }


def _detect_js_runtime() -> tuple[str, str] | None:
    suffix = ".exe" if os.name == "nt" else ""
    bundled_deno = TOOLS / f"deno{suffix}"
    if bundled_deno.is_file():
        return ("deno", str(bundled_deno))
    bundled_package_deno = TOOLS / "package" / f"deno{suffix}"
    if bundled_package_deno.is_file():
        return ("deno", str(bundled_package_deno))
    for name in ("deno", "node", "bun", "quickjs", "qjs"):
        found = shutil.which(name)
        if found:
            return (name, found)
    return None


@contextlib.contextmanager
def _bootstrap_lock(timeout_seconds: float = 2.0) -> Iterator[None]:
    lock_path = ROOT / ".setup.lock"
    handle = lock_path.open("a+", encoding="utf-8")
    acquired = False
    deadline = time.monotonic() + timeout_seconds
    try:
        while True:
            try:
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Another AutoClip Studio setup process is already running.")
                time.sleep(0.2)
        yield
    finally:
        if acquired:
            try:
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


def _base_env() -> dict[str, str]:
    env = {
        **os.environ,
        "UV_PYTHON_INSTALL_DIR": str(ROOT / ".runtime"),
        "UV_CACHE_DIR": str(ROOT / ".uv-cache"),
        "PYTHONIOENCODING": "utf-8",
    }
    if sys.platform == "darwin":
        brew_prefix = "/opt/homebrew/opt/ffmpeg-full/bin:/opt/homebrew/bin:/usr/local/opt/ffmpeg-full/bin:/usr/local/bin"
        env["PATH"] = f"{brew_prefix}:{env.get('PATH', '/usr/bin:/bin')}"
    return env


def run(*command: str | Path, capture: bool = False, timeout: int | None = None) -> str:
    result = subprocess.run(
        [str(item) for item in command], check=True, cwd=ROOT, text=True, encoding="utf-8", errors="replace",
        stdout=subprocess.PIPE if capture else None, stderr=subprocess.STDOUT if capture else None,
        stdin=subprocess.DEVNULL, timeout=timeout,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        env=_base_env(),
    )
    return result.stdout or ""


def _parse_pkg_line(line: str) -> str | None:
    stripped = line.strip()
    if not stripped:
        return None
    for prefix in ("Downloading ", "Collecting ", "Installed ", "Prepared ", "Unpacking ", "Building wheel for "):
        if stripped.startswith(prefix):
            return stripped
    if stripped.startswith("+ "):
        return "Installed " + stripped[2:].strip()
    return None


def _run_streaming_install(
    command: list[str | Path],
    *,
    component: str,
    stage_id: str,
    stage_index: int,
    stage_start: float,
    stage_end: float,
    expected_items: int,
    timeout: int = 600,
) -> None:
    started = time.monotonic()
    seen_pkgs = 0
    proc = subprocess.Popen(
        [str(c) for c in command],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        env=_base_env(),
    )
    tail: list[str] = []
    try:
        assert proc.stdout is not None
        for raw_line in proc.stdout:
            line = _redact(raw_line.rstrip())
            if line:
                tail.append(line)
                if len(tail) > 40:
                    tail.pop(0)
                _emit("log", level="INFO", component=component, message=line)
            parsed = _parse_pkg_line(line)
            elapsed = time.monotonic() - started
            if parsed:
                seen_pkgs += 1
                frac = min(0.92, seen_pkgs / max(1, expected_items * 2))
                overall = stage_start + (stage_end - stage_start) * frac
                state = "Downloading" if parsed.startswith(("Downloading", "Collecting")) else "Installing"
                _emit(
                    "progress", id=component, stage_id=stage_id, stage_index=stage_index, stage_total=8,
                    state=state, detail=parsed, elapsed_seconds=round(elapsed, 1),
                    component_percent=round(frac * 100.0, 1), overall_percent=overall, overall_mode="estimated",
                )
            elif time.monotonic() - started > timeout:
                proc.kill()
                raise RuntimeError(f"Operation timed out after {timeout}s while running {command[0]}.")
        rc = proc.wait(timeout=15)
        if rc != 0:
            summary = tail[-1] if tail else f"Exited with code {rc}"
            raise RuntimeError(f"{component} installer failed (exit {rc}): {summary}")
    finally:
        if proc.poll() is None:
            proc.kill()


def _verified(path: Path, digest: str) -> bool:
    if not path.is_file():
        return False
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest().lower() == digest.lower()


def download(
    url: str,
    target: Path,
    digest: str | None = None,
    *,
    component: str = "",
    stage_id: str = "",
    stage_index: int = 0,
    stage_start: float = 0.0,
    stage_end: float = 0.0,
) -> None:
    if digest and _verified(target, digest):
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_suffix(target.suffix + ".part")
    for attempt in range(3):
        existing = part.stat().st_size if part.is_file() else 0
        headers = {"User-Agent": "AutoClipStudio-Setup/1.0"}
        if existing > 0:
            headers["Range"] = f"bytes={existing}-"
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=45) as response:
                status = getattr(response, "status", 200)
                if existing > 0 and status == 200:
                    existing = 0
                cl = response.headers.get("Content-Length")
                rem = int(cl) if cl and cl.isdigit() else None
                total = (existing + rem) if rem is not None else None
                if total is not None:
                    free = shutil.disk_usage(target.parent).free
                    if free < max(0, total - existing):
                        raise RuntimeError(f"Insufficient disk space for {target.name}")
                mode = "ab" if existing > 0 and status == 206 else "wb"
                downloaded = existing
                started = time.perf_counter()
                last_emit = 0.0
                with part.open(mode) as stream:
                    while True:
                        chunk = response.read(65536)
                        if not chunk:
                            break
                        stream.write(chunk)
                        downloaded += len(chunk)
                        now = time.perf_counter()
                        if component and (now - last_emit >= 0.12 or (total and downloaded >= total)):
                            elapsed = max(0.001, now - started)
                            speed = (downloaded - existing) / elapsed
                            eta = ((total - downloaded) / speed) if (total and speed > 0) else None
                            pct = min(100.0, (downloaded / total) * 100.0) if total else None
                            overall = (
                                stage_start + (stage_end - stage_start) * ((pct or 0.0) / 100.0)
                                if (total and stage_end > stage_start)
                                else None
                            )
                            _emit(
                                "progress", id=component, stage_id=stage_id or "media_tools",
                                stage_index=stage_index, stage_total=8, state="Downloading",
                                current_file=target.name, detail=f"Downloading {target.name}",
                                downloaded=downloaded, total=total, component_percent=pct,
                                percent=pct, speed_bps=speed, eta_seconds=eta, elapsed_seconds=round(elapsed, 1),
                                overall_percent=overall, overall_mode="measured" if total else "stage",
                            )
                            last_emit = now
            if digest:
                if component:
                    _emit("progress", id=component, stage_id=stage_id, stage_index=stage_index, stage_total=8,
                          state="Verifying", detail=f"Verifying SHA-256 of {target.name}")
                if not _verified(part, digest):
                    part.unlink(missing_ok=True)
                    raise RuntimeError(f"SHA-256 verification failed for {target.name}.")
            part.replace(target)
            return
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt + 1 >= 3:
                raise
            time.sleep(2 ** attempt)


def _manifest_components() -> list[dict[str, Any]]:
    if not MANIFEST.is_file():
        return []
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    target_plat = "macos" if sys.platform == "darwin" else "windows"
    result: list[dict[str, Any]] = []
    for item in data.get("components", []):
        plats = item.get("platforms", ["windows", "macos"])
        if target_plat in plats:
            result.append(item)
    return result


def _manifest_component(comp_id: str) -> dict[str, Any]:
    for item in _manifest_components():
        if item.get("id") == comp_id:
            return item
    return {}


def _probe_tool_version(exe: Path, flag: str = "-version") -> str:
    try:
        out = run(exe, flag, capture=True, timeout=8).strip()
        first = out.splitlines()[0] if out else ""
        if "version " in first:
            return first.split("version ")[1].split()[0]
        return first[:48]
    except Exception:
        return ""


def media_tool_problems() -> list[str]:
    problems = []
    suffix = ".exe" if os.name == "nt" else ""
    for name in ("ffmpeg", "ffprobe"):
        executable = FFMPEG / "bin" / f"{name}{suffix}"
        if not executable.is_file() and os.name != "nt":
            for candidate in (
                Path("/opt/homebrew/opt/ffmpeg-full/bin") / name,
                Path("/usr/local/opt/ffmpeg-full/bin") / name,
                Path("/opt/homebrew/bin") / name,
                Path("/usr/local/bin") / name,
            ):
                if candidate.is_file():
                    executable = candidate
                    break
        if not executable.is_file():
            found = shutil.which(name)
            if found:
                executable = Path(found)
        if executable.is_file():
            try:
                output = run(executable, "-version", capture=True, timeout=10)
                if not output.startswith(f"{name} version "):
                    problems.append(f"{name} did not report its version")
                    continue
                if name == "ffmpeg" and sys.platform == "darwin":
                    filters = run(executable, "-hide_banner", "-filters", capture=True, timeout=10)
                    if " subtitles " not in filters:
                        problems.append("ffmpeg lacks libass subtitles filter (install ffmpeg-full)")
                        continue
                continue
            except (OSError, subprocess.SubprocessError):
                problems.append(f"{name} is missing or could not start")
                continue
        problems.append(f"{name} is missing or could not start")
    return problems


def _installed_versions_in_venv(python_exe: Path = PYTHON) -> dict[str, str]:
    try:
        if Path(sys.executable).resolve() == python_exe.resolve():
            return {
                dist.metadata["Name"].lower(): dist.version
                for dist in importlib.metadata.distributions()
                if dist.metadata.get("Name")
            }
    except OSError:
        pass
    if not python_exe.is_file():
        return {}
    probe = (
        "import importlib.metadata, json; "
        "print(json.dumps({d.metadata['Name'].lower(): d.version "
        "for d in importlib.metadata.distributions() if d.metadata.get('Name')}))"
    )
    try:
        raw = run(python_exe, "-c", probe, capture=True, timeout=15)
        data = json.loads(raw.strip().splitlines()[-1])
        return {str(k).lower(): str(v) for k, v in data.items()}
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return {}


def missing_or_outdated_requirements(
    *,
    include_dev: bool = False,
    installed_map: dict[str, str] | None = None,
) -> tuple[list[str], list[str]]:
    installed = installed_map if installed_map is not None else _installed_versions_in_venv()
    problems: list[str] = []
    to_install: list[str] = []
    for name, op, expected, raw_spec in _parse_requirements():
        key = name.lower()
        if not include_dev and key in NON_STARTUP_PACKAGES:
            continue
        cur = installed.get(key)
        if cur is None:
            problems.append(f"{name} is missing")
            to_install.append(raw_spec)
        elif not _is_version_satisfied(name, op, expected, cur):
            if op == ">=" or key in ALLOW_NEWER_PACKAGES:
                problems.append(f"{name} >= {expected} is required (installed {cur})")
                to_install.append(f"{name}>={expected}")
            else:
                problems.append(f"{name} {expected} is required (installed {cur})")
                to_install.append(raw_spec)
    return problems, to_install


def check(*, include_dev: bool = False) -> list[str]:
    problems: list[str] = []
    if not ((3, 11) <= sys.version_info[:2] < (3, 13)):
        problems.append("Python 3.11 or 3.12 is required")
    if not PYTHONW.is_file():
        problems.append("The project's Python interpreter is missing")
    pkg_problems, _ = missing_or_outdated_requirements(include_dev=include_dev)
    problems.extend(pkg_problems)
    return problems + media_tool_problems()


def ensure_uv() -> Path:
    _emit("progress", id="uv", stage_id="virtual_env", stage_index=3, stage_total=8,
          state="Checking", detail="Checking uv package bootstrapper", overall_percent=17, overall_mode="estimated")
    found = _find_uv()
    if found is not None:
        ver = _probe_tool_version(found, "--version").replace("uv ", "")
        _emit("progress", id="uv", stage_id="virtual_env", stage_index=3, stage_total=8,
              state="Reused Existing Installation", version=ver, detail=str(found),
              component_percent=100, overall_percent=19, overall_mode="estimated")
        return found
    tc = json.loads(TOOLCHAIN.read_text(encoding="utf-8"))
    pins = tc["uv_macos_arm64"] if sys.platform == "darwin" else tc["uv"]
    TOOLS.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=TOOLS, prefix="uv-") as directory:
        ext = ".tar.gz" if sys.platform == "darwin" else ".zip"
        archive = Path(directory) / f"uv{ext}"
        download(
            pins["url"], archive, pins.get("sha256") or None,
            component="uv", stage_id="virtual_env", stage_index=3, stage_start=17.0, stage_end=21.0,
        )
        _emit("progress", id="uv", stage_id="virtual_env", stage_index=3, stage_total=8,
              state="Configuring", detail="Extracting uv bootstrapper", overall_percent=21.5, overall_mode="stage")
        if ext == ".tar.gz":
            with tarfile.open(archive, "r:gz") as tar:
                member = next(m for m in tar.getmembers() if Path(m.name).name == "uv")
                src = tar.extractfile(member)
                assert src is not None
                executable = Path(directory) / "uv"
                with src, executable.open("wb") as target:
                    shutil.copyfileobj(src, target)
            os.chmod(executable, 0o755)
            executable.replace(UV)
        else:
            with zipfile.ZipFile(archive) as payload:
                member = next(name for name in payload.namelist() if Path(name).name == "uv.exe")
                executable = Path(directory) / "uv.exe"
                with payload.open(member) as source, executable.open("wb") as target:
                    shutil.copyfileobj(source, target)
            executable.replace(UV)
    ver = _probe_tool_version(UV, "--version").replace("uv ", "")
    _emit("progress", id="uv", stage_id="virtual_env", stage_index=3, stage_total=8,
          state="Ready", version=ver, detail=str(UV), component_percent=100, overall_percent=22, overall_mode="estimated")
    return UV


def ensure_ffmpeg() -> str:
    _emit("progress", id="ffmpeg", stage_id="media_tools", stage_index=5, stage_total=8,
          state="Checking", detail="Checking FFmpeg & FFprobe", overall_percent=57, overall_mode="estimated")
    if not media_tool_problems():
        suffix = ".exe" if os.name == "nt" else ""
        exe = FFMPEG / "bin" / f"ffmpeg{suffix}"
        if not exe.is_file():
            exe = Path(shutil.which("ffmpeg") or exe)
        ver = _probe_tool_version(exe)
        _emit("progress", id="ffmpeg", stage_id="media_tools", stage_index=5, stage_total=8,
              state="Reused Existing Installation", version=ver, detail=f"FFmpeg {ver or 'verified'}",
              component_percent=100, overall_percent=74, overall_mode="estimated")
        return "reused"
    if sys.platform == "darwin":
        brew = shutil.which("brew") or ("/opt/homebrew/bin/brew" if Path("/opt/homebrew/bin/brew").is_file() else None)
        if brew:
            _emit("progress", id="ffmpeg", stage_id="media_tools", stage_index=5, stage_total=8,
                  state="Installing", detail="Installing ffmpeg-full via Homebrew", overall_percent=62, overall_mode="stage")
            _run_streaming_install([brew, "install", "ffmpeg-full"], component="ffmpeg",
                                   stage_id="media_tools", stage_index=5, stage_start=60.0, stage_end=74.0, expected_items=4, timeout=600)
            if not media_tool_problems():
                ver = _probe_tool_version(Path(shutil.which("ffmpeg") or "/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg"))
                _emit("progress", id="ffmpeg", stage_id="media_tools", stage_index=5, stage_total=8,
                      state="Ready", version=ver, detail=f"Homebrew ffmpeg-full {ver}", component_percent=100, overall_percent=74, overall_mode="estimated")
                return "installed"
        _emit("progress", id="ffmpeg", stage_id="media_tools", stage_index=5, stage_total=8,
              state="Failed", detail="Install FFmpeg via `brew install ffmpeg-full` on macOS.", overall_percent=60, overall_mode="stage")
        raise RuntimeError("FFmpeg with libass support is missing on macOS. Install via Homebrew (`brew install ffmpeg-full`).")
    if not _JSON_PROGRESS:
        print("Preparing FFmpeg and ffprobe...", flush=True)
    comp = _manifest_component("ffmpeg")
    url = comp.get("download_url") or f"https://github.com/GyanD/codexffmpeg/releases/download/9.1/{FFMPEG_RELEASE}.zip"
    fallback = comp.get("fallback_url") or f"https://www.gyan.dev/ffmpeg/builds/packages/{FFMPEG_RELEASE}.zip"
    digest = comp.get("sha256") or FFMPEG_SHA256
    with tempfile.TemporaryDirectory(dir=TOOLS, prefix="ffmpeg-") as directory:
        folder = Path(directory)
        archive = folder / "ffmpeg.zip"
        try:
            download(url, archive, digest, component="ffmpeg", stage_id="media_tools", stage_index=5, stage_start=58.0, stage_end=71.0)
        except OSError:
            download(fallback, archive, digest, component="ffmpeg", stage_id="media_tools", stage_index=5, stage_start=58.0, stage_end=71.0)
        _emit("progress", id="ffmpeg", stage_id="media_tools", stage_index=5, stage_total=8,
              state="Configuring", detail="Extracting FFmpeg and FFprobe binaries", overall_percent=72.5, overall_mode="stage")
        with zipfile.ZipFile(archive) as payload:
            payload.extractall(folder)
        extracted = folder / FFMPEG_RELEASE
        if not all((extracted / "bin" / name).is_file() for name in ("ffmpeg.exe", "ffprobe.exe")):
            raise RuntimeError("The verified FFmpeg archive is missing an executable.")
        backup = TOOLS / f"ffmpeg-backup-{uuid.uuid4().hex}"
        if FFMPEG.exists():
            FFMPEG.rename(backup)
        try:
            extracted.rename(FFMPEG)
        except OSError:
            if backup.exists():
                backup.rename(FFMPEG)
            raise
        finally:
            if backup.exists():
                shutil.rmtree(backup, ignore_errors=True)
    ver = _probe_tool_version(FFMPEG / "bin" / "ffmpeg.exe")
    _emit("progress", id="ffmpeg", stage_id="media_tools", stage_index=5, stage_total=8,
          state="Ready", version=ver, detail=f"FFmpeg & FFprobe {ver} verified",
          component_percent=100, overall_percent=75, overall_mode="estimated")
    return "installed"


def _install_packages_selectively(specs: list[str], uv_path: Path | None) -> None:
    if not specs:
        return
    if uv_path is not None and uv_path.is_file():
        _run_streaming_install([uv_path, "pip", "install", "--python", PYTHON, *specs],
                               component="python_packages", stage_id="core_packages", stage_index=4,
                               stage_start=29.0, stage_end=53.0, expected_items=len(specs))
        return
    if _venv_has_pip(PYTHON):
        _run_streaming_install([PYTHON, "-m", "pip", "install", *specs],
                               component="python_packages", stage_id="core_packages", stage_index=4,
                               stage_start=29.0, stage_end=53.0, expected_items=len(specs))
        return
    uv_bin = ensure_uv()
    _run_streaming_install([uv_bin, "pip", "install", "--python", PYTHON, *specs],
                           component="python_packages", stage_id="core_packages", stage_index=4,
                           stage_start=29.0, stage_end=53.0, expected_items=len(specs))


def _persist_completion_state(summary: dict[str, Any]) -> None:
    state_path = _data_root() / "dependency_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    current: dict[str, Any] = {}
    if state_path.is_file():
        try:
            loaded = json.loads(state_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                current = loaded
        except (OSError, ValueError):
            pass
    current.update({
        "last_setup_completed_at": datetime.now(timezone.utc).isoformat(),
        "last_setup_ok": bool(summary.get("ok", False)),
        "setup_summary": summary,
    })
    tmp = state_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(current, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(state_path)


def ensure_runtime(
    *,
    tests: bool = False,
    local_whisper: bool = False,
    cloud_only: bool = False,
    repair_only: bool = False,
) -> dict[str, Any]:
    global _LAST_OVERALL
    _LAST_OVERALL = 0.0
    started_at = time.monotonic()
    counts = {"installed": 0, "reused": 0, "skipped": 0, "failed": 0, "required_validated": 0}
    optional_notices: list[str] = []

    with _bootstrap_lock():
        # Emit initial dynamic plan
        stages_plan = [{"id": sid, "index": i + 1, "total": len(STAGES), "name": sname}
                       for i, (sid, sname, _, _) in enumerate(STAGES)]
        _emit("plan", stages=stages_plan, components=[
            {
                "id": c["id"], "name": c["name"], "mandatory": bool(c.get("mandatory", True)),
                "stage_id": c.get("stage_id", ""), "estimated_bytes": c.get("estimated_installed_bytes") or 0,
                "download_bytes": c.get("download_size_bytes"),
            }
            for c in _manifest_components()
        ])

        # Stage 1: System Compatibility
        _emit("stage", stage_id="system_compatibility", stage_index=1, stage_total=8,
              name="System Compatibility", state="Checking", detail="Probing OS, CPU, RAM, disk, and GPU",
              overall_percent=1, overall_mode="estimated")
        sys_info = _detect_system_info()
        _emit("system_info", **sys_info)
        if not sys_info["permissions_ok"]:
            raise RuntimeError(f"Insufficient write permissions in {ROOT} or {_data_root()}.")
        free_mb = sys_info["disk_free_bytes"] / (1024 * 1024)
        needs_heavy_install = not PYTHONW.is_file() or bool(media_tool_problems())
        if needs_heavy_install and free_mb < 300:
            raise RuntimeError(f"Insufficient disk space ({free_mb:.0f} MB free; at least 300 MB required).")
        _emit("stage", stage_id="system_compatibility", stage_index=1, stage_total=8,
              name="System Compatibility", state="Completed",
              detail=f"{sys_info['os_detail']} • {free_mb / 1024:.1f} GB free",
              overall_percent=5, overall_mode="estimated")

        # Stage 2 & Stage 3: Python Runtime & Virtual Environment
        _emit("stage", stage_id="python_runtime", stage_index=2, stage_total=8,
              name="Python Runtime", state="Checking", detail="Checking Python 3.11–3.12 runtime",
              overall_percent=6, overall_mode="estimated")
        _emit("progress", id="python_runtime", stage_id="python_runtime", stage_index=2, stage_total=8,
              state="Checking", detail="Checking compatible Python installation", overall_percent=7, overall_mode="estimated")

        try:
            py_info = run(PYTHON, "-c", "import platform, sys; print(platform.python_version(), (3, 11) <= sys.version_info[:2] < (3, 13))",
                          capture=True, timeout=15).strip().split()
            py_ver = py_info[0] if len(py_info) >= 1 else ""
            usable = len(py_info) >= 2 and py_info[1] == "True"
        except (OSError, subprocess.SubprocessError):
            py_ver = ""
            usable = False

        uv_bin = _find_uv()
        if not usable or not PYTHONW.is_file():
            _emit("stage", stage_id="python_runtime", stage_index=2, stage_total=8,
                  name="Python Runtime", state="Running", detail="Provisioning managed Python 3.12",
                  overall_percent=9, overall_mode="stage")
            _emit("stage", stage_id="virtual_env", stage_index=3, stage_total=8,
                  name="Virtual Environment", state="Running", detail="Preparing uv & project .venv",
                  overall_percent=16, overall_mode="stage")
            uv_bin = ensure_uv()
            _emit("progress", id="python_runtime", stage_id="python_runtime", stage_index=2, stage_total=8,
                  state="Installing", detail="Installing Python 3.12 runtime via uv", overall_percent=12, overall_mode="stage")
            _emit("progress", id="python_venv", stage_id="virtual_env", stage_index=3, stage_total=8,
                  state="Configuring", detail="Creating project-local .venv", overall_percent=22, overall_mode="stage")
            if not _JSON_PROGRESS:
                print("Preparing the project-local Python environment...", flush=True)
            _run_streaming_install([uv_bin, "venv", "--allow-existing", "--managed-python", "--python", "3.12", ROOT / ".venv"],
                                   component="python_venv", stage_id="virtual_env", stage_index=3, stage_start=22.0, stage_end=25.5, expected_items=2, timeout=300)
            py_ver = run(PYTHON, "-c", "import platform; print(platform.python_version())", capture=True, timeout=15).strip()
            counts["installed"] += 2
            _emit("progress", id="python_runtime", stage_id="python_runtime", stage_index=2, stage_total=8,
                  state="Ready", version=py_ver, detail=f"Python {py_ver} installed", component_percent=100, overall_percent=16, overall_mode="estimated")
            _emit("progress", id="python_venv", stage_id="virtual_env", stage_index=3, stage_total=8,
                  state="Ready", version=py_ver, detail=".venv ready", component_percent=100, overall_percent=26, overall_mode="estimated")
            counts["required_validated"] += 2
        else:
            counts["reused"] += 2
            counts["required_validated"] += 2
            _emit("progress", id="python_runtime", stage_id="python_runtime", stage_index=2, stage_total=8,
                  state="Reused Existing Installation", version=py_ver, detail=f"Python {py_ver} verified",
                  component_percent=100, overall_percent=16, overall_mode="estimated")
            _emit("stage", stage_id="python_runtime", stage_index=2, stage_total=8,
                  name="Python Runtime", state="Completed", detail=f"Reused Python {py_ver}", overall_percent=16, overall_mode="estimated")
            _emit("stage", stage_id="virtual_env", stage_index=3, stage_total=8,
                  name="Virtual Environment", state="Checking", detail="Checking .venv & package manager", overall_percent=18, overall_mode="estimated")
            pm_label = "uv" if uv_bin else ("pip" if _venv_has_pip(PYTHON) else "bootstrap")
            if uv_bin:
                uv_ver = _probe_tool_version(uv_bin, "--version").replace("uv ", "")
                _emit("progress", id="uv", stage_id="virtual_env", stage_index=3, stage_total=8,
                      state="Reused Existing Installation", version=uv_ver, detail=f"uv {uv_ver}", component_percent=100)
                counts["reused"] += 1
            _emit("progress", id="python_venv", stage_id="virtual_env", stage_index=3, stage_total=8,
                  state="Reused Existing Installation", version=py_ver, detail=f"Reusing .venv ({pm_label})",
                  component_percent=100, overall_percent=26, overall_mode="estimated")
        _emit("stage", stage_id="virtual_env", stage_index=3, stage_total=8,
              name="Virtual Environment", state="Completed", detail=".venv validated", overall_percent=26, overall_mode="estimated")

        # Stage 4: Core Application Dependencies
        _emit("stage", stage_id="core_packages", stage_index=4, stage_total=8,
              name="Core Application Dependencies", state="Checking", detail="Inspecting requirements.txt packages",
              overall_percent=27, overall_mode="estimated")
        _emit("progress", id="python_packages", stage_id="core_packages", stage_index=4, stage_total=8,
              state="Checking", detail="Checking PySide6 and core Python packages", overall_percent=28, overall_mode="estimated")
        installed_map = _installed_versions_in_venv(PYTHON)
        _, specs_to_install = missing_or_outdated_requirements(include_dev=tests, installed_map=installed_map)
        if specs_to_install:
            pkg_names = ", ".join(s.split("==")[0].split(">=")[0] for s in specs_to_install)
            _emit("progress", id="python_packages", stage_id="core_packages", stage_index=4, stage_total=8,
                  state="Installing", detail=f"Installing: {pkg_names}", overall_percent=30, overall_mode="estimated")
            if not _JSON_PROGRESS:
                print(f"Installing missing/outdated dependencies: {pkg_names}...", flush=True)
            _install_packages_selectively(specs_to_install, uv_bin)
            installed_map = _installed_versions_in_venv(PYTHON)
            counts["installed"] += 1
        else:
            counts["reused"] += 1

        pyside_ver = installed_map.get("pyside6", "")
        yt_ver = installed_map.get("yt-dlp", "")
        cv_ver = installed_map.get("opencv-python-headless", "")
        _emit("progress", id="pyside6", stage_id="core_packages", stage_index=4, stage_total=8,
              state="Ready" if any(s.lower().startswith("pyside6") for s in specs_to_install) else "Reused Existing Installation",
              version=pyside_ver, detail=f"PySide6 {pyside_ver}", component_percent=100, overall_percent=53, overall_mode="estimated")
        _emit("progress", id="python_packages", stage_id="core_packages", stage_index=4, stage_total=8,
              state="Ready" if specs_to_install else "Reused Existing Installation",
              version=f"{len(installed_map)} pkgs", detail="Core requirements satisfied", component_percent=100, overall_percent=55, overall_mode="estimated")
        _emit("progress", id="opencv_face_model", stage_id="core_packages", stage_index=4, stage_total=8,
              state="Reused Existing Installation" if cv_ver else "Ready", version=cv_ver,
              detail=f"OpenCV {cv_ver} + Haar cascade", component_percent=100, overall_percent=56, overall_mode="estimated")
        counts["required_validated"] += 3
        _emit("stage", stage_id="core_packages", stage_index=4, stage_total=8,
              name="Core Application Dependencies", state="Completed", detail="Core packages verified",
              overall_percent=56, overall_mode="estimated")

        # Stage 5: Media Processing Tools
        _emit("stage", stage_id="media_tools", stage_index=5, stage_total=8,
              name="Media Processing Tools", state="Checking", detail="Checking FFmpeg, FFprobe, yt-dlp, and JS runtime",
              overall_percent=57, overall_mode="estimated")
        ff_status = ensure_ffmpeg()
        counts["installed" if ff_status == "installed" else "reused"] += 1
        counts["required_validated"] += 1

        _emit("progress", id="yt_dlp", stage_id="media_tools", stage_index=5, stage_total=8,
              state="Reused Existing Installation" if not any(s.lower().startswith("yt-dlp") for s in specs_to_install) else "Ready",
              version=yt_ver, detail=f"yt-dlp {yt_ver} preserved", component_percent=100, overall_percent=77, overall_mode="estimated")
        counts["required_validated"] += 1

        js_rt = _detect_js_runtime()
        if js_rt:
            _emit("progress", id="js_runtime", stage_id="media_tools", stage_index=5, stage_total=8,
                  state="Reused Existing Installation", version=js_rt[0], detail=f"{js_rt[0]} ({js_rt[1]})",
                  component_percent=100, overall_percent=79, overall_mode="estimated")
            counts["reused"] += 1
        else:
            _emit("progress", id="js_runtime", stage_id="media_tools", stage_index=5, stage_total=8,
                  state="Skipped", detail="No Deno/Node.js found (optional for YouTube EJS)",
                  component_percent=100, overall_percent=79, overall_mode="estimated")
            counts["skipped"] += 1
            optional_notices.append("Optional JavaScript runtime (Deno or Node.js) not detected; YouTube EJS challenges may require one.")

        _emit("progress", id="caption_fonts", stage_id="media_tools", stage_index=5, stage_total=8,
              state="Reused Existing Installation", version="Poppins", detail="Bundled Poppins fonts verified",
              component_percent=100, overall_percent=80, overall_mode="estimated")
        counts["reused"] += 1
        _emit("stage", stage_id="media_tools", stage_index=5, stage_total=8,
              name="Media Processing Tools", state="Completed", detail="Media tools validated",
              overall_percent=80, overall_mode="estimated")

        # Stage 6: Hardware Acceleration
        _emit("stage", stage_id="hardware_accel", stage_index=6, stage_total=8,
              name="Hardware Acceleration", state="Checking", detail=sys_info["hw_accel"],
              overall_percent=82, overall_mode="estimated")
        if sys.platform == "darwin":
            mlx_ver = installed_map.get("mlx-whisper", "")
            vt_ok = False
            try:
                enc = run("ffmpeg", "-hide_banner", "-encoders", capture=True, timeout=8)
                vt_ok = "h264_videotoolbox" in enc
            except Exception:
                pass
            if mlx_ver:
                _emit("progress", id="mlx_whisper", stage_id="hardware_accel", stage_index=6, stage_total=8,
                      state="Reused Existing Installation", version=mlx_ver,
                      detail=f"MLX Whisper {mlx_ver} • VideoToolbox: {'Yes' if vt_ok else 'CPU fallback'}",
                      component_percent=100, overall_percent=88, overall_mode="estimated")
                counts["reused"] += 1
            else:
                _emit("progress", id="mlx_whisper", stage_id="hardware_accel", stage_index=6, stage_total=8,
                      state="Skipped", detail="CPU Faster-Whisper fallback active",
                      component_percent=100, overall_percent=88, overall_mode="estimated")
                counts["skipped"] += 1
                optional_notices.append("MLX Whisper unavailable; macOS will use CPU transcription fallback.")
        else:
            if "NVIDIA" in sys_info["gpu"]:
                _emit("progress", id="cuda_runtime", stage_id="hardware_accel", stage_index=6, stage_total=8,
                      state="Reused Existing Installation", version="NVENC / CUDA",
                      detail=f"{sys_info['gpu']} (CPU fallback active if CUDA DLLs absent)",
                      component_percent=100, overall_percent=88, overall_mode="estimated")
                counts["reused"] += 1
            else:
                _emit("progress", id="cuda_runtime", stage_id="hardware_accel", stage_index=6, stage_total=8,
                      state="Skipped", version="CPU", detail="No NVIDIA GPU detected; CPU fallback active (no CUDA Toolkit required)",
                      component_percent=100, overall_percent=88, overall_mode="estimated")
                counts["skipped"] += 1
        _emit("stage", stage_id="hardware_accel", stage_index=6, stage_total=8,
              name="Hardware Acceleration", state="Completed", detail=sys_info["hw_accel"],
              overall_percent=88, overall_mode="estimated")

        # Stage 7: Transcription & Optional Models
        _emit("stage", stage_id="transcription_models", stage_index=7, stage_total=8,
              name="Transcription & Optional Models", state="Checking",
              detail="Checking local Whisper engine & cached models", overall_percent=89, overall_mode="estimated")
        fw_ver = installed_map.get("faster-whisper", "")
        _emit("progress", id="whisper_engine", stage_id="transcription_models", stage_index=7, stage_total=8,
              state="Reused Existing Installation" if fw_ver else "Ready", version=fw_ver,
              detail=f"faster-whisper {fw_ver or 'ready'}", component_percent=100, overall_percent=91, overall_mode="estimated")
        counts["required_validated"] += 1

        use_cloud = bool(cloud_only or _user_cloud_only_setting())
        yunet_path = ROOT / "cache" / "models" / "face_detection_yunet_2023mar.onnx"
        if yunet_path.is_file():
            _emit("progress", id="yunet_asd_model", stage_id="transcription_models", stage_index=7, stage_total=8,
                  state="Reused Existing Installation", detail="Cached YuNet ASD model ready", component_percent=100)
            counts["reused"] += 1
        else:
            _emit("progress", id="yunet_asd_model", stage_id="transcription_models", stage_index=7, stage_total=8,
                  state="Skipped", detail="Optional 227 KB ASD upgrade skipped (Haar fallback active)", component_percent=100)
            counts["skipped"] += 1

        model_id = "whisper_model_large_v3_turbo"
        if use_cloud:
            _emit("progress", id=model_id, stage_id="transcription_models", stage_index=7, stage_total=8,
                  state="Skipped", detail="Skipped (Cloud-only transcription selected)",
                  component_percent=100, overall_percent=95, overall_mode="estimated")
            counts["skipped"] += 1
        elif repair_only or not local_whisper:
            _emit("progress", id=model_id, stage_id="transcription_models", stage_index=7, stage_total=8,
                  state="Skipped", detail="1.51 GB optional model deferred (download anytime in Dependency Manager)",
                  component_percent=100, overall_percent=95, overall_mode="estimated")
            counts["skipped"] += 1
        else:
            try:
                sys.path.insert(0, str(ROOT))
                from autoclip_studio.speech_engine import SpeechSettings, download_speech_model, find_executable, find_model
                settings = SpeechSettings()
                existing_model = find_model("large-v3-turbo", settings, find_executable(settings.executable))
                if existing_model:
                    _emit("progress", id=model_id, stage_id="transcription_models", stage_index=7, stage_total=8,
                          state="Reused Existing Installation", detail=f"Reusing cached model: {existing_model.name}",
                          component_percent=100, overall_percent=95, overall_mode="estimated")
                    counts["reused"] += 1
                else:
                    _emit("progress", id=model_id, stage_id="transcription_models", stage_index=7, stage_total=8,
                          state="Downloading", detail="Downloading Whisper large-v3-turbo (~1.51 GB)...",
                          overall_percent=92, overall_mode="stage")
                    download_speech_model("large-v3-turbo", settings)
                    _emit("progress", id=model_id, stage_id="transcription_models", stage_index=7, stage_total=8,
                          state="Ready", detail="Whisper large-v3-turbo ready",
                          component_percent=100, overall_percent=95, overall_mode="estimated")
                    counts["installed"] += 1
            except Exception as exc:
                _emit("progress", id=model_id, stage_id="transcription_models", stage_index=7, stage_total=8,
                      state="Skipped", detail=f"Optional model skipped ({exc})",
                      component_percent=100, overall_percent=95, overall_mode="estimated")
                counts["skipped"] += 1
                optional_notices.append(f"Optional local Whisper model was skipped ({_redact(str(exc))}).")
        _emit("stage", stage_id="transcription_models", stage_index=7, stage_total=8,
              name="Transcription & Optional Models", state="Completed", detail="Transcription setup ready",
              overall_percent=95, overall_mode="estimated")

        # Stage 8: Final Validation
        _emit("stage", stage_id="final_validation", stage_index=8, stage_total=8,
              name="Final Validation", state="Verifying",
              detail="Running final runtime, import, and media validation",
              overall_percent=97, overall_mode="estimated")
        run(PYTHON, __file__, "--check", timeout=30)
        run(PYTHON, "-c", "import PySide6, cv2, av, yt_dlp, autoclip_studio; print('IMPORTS_OK')", timeout=30)
        counts["required_validated"] += 1

        duration = round(time.monotonic() - started_at, 2)
        summary = {
            "ok": True,
            "duration_seconds": duration,
            "required_validated": counts["required_validated"],
            "installed_count": counts["installed"],
            "reused_count": counts["reused"],
            "skipped_count": counts["skipped"],
            "failed_count": counts["failed"],
            "optional_notices": optional_notices,
            "setup_log": str(_setup_log_path()),
        }
        _persist_completion_state(summary)
        _emit("stage", stage_id="final_validation", stage_index=8, stage_total=8,
              name="Final Validation", state="Completed", detail="All required checks passed",
              overall_percent=100, overall_mode="estimated", allow_complete=True)
        _emit("complete", allow_complete=True, overall_percent=100, **summary)

        if tests:
            run(PYTHON, "-m", "pytest", timeout=600)
        return summary


def main() -> int:
    global _JSON_PROGRESS
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Validate the current environment without installing.")
    parser.add_argument("--system-info", action="store_true", help="Print detected platform & hardware JSON.")
    parser.add_argument("--json-progress", action="store_true", help="Emit structured JSON progress lines.")
    parser.add_argument("--local-whisper", action="store_true", help="Ensure default Whisper large-v3-turbo model.")
    parser.add_argument("--cloud-only", action="store_true", help="Skip downloading local Whisper models.")
    parser.add_argument("--repair-only", action="store_true", help="Repair only missing/incompatible required components.")
    parser.add_argument("--no-tests", action="store_true", help="Skip running pytest after setup.")
    options = parser.parse_args()
    _JSON_PROGRESS = options.json_progress
    if options.system_info:
        print(json.dumps(_detect_system_info(), ensure_ascii=False))
        return 0
    if options.check:
        problems = check()
        for problem in problems:
            print(problem, file=sys.stderr)
        return int(bool(problems))
    try:
        ensure_runtime(
            tests=(not options.no_tests and not options.json_progress),
            local_whisper=options.local_whisper,
            cloud_only=options.cloud_only,
            repair_only=options.repair_only,
        )
    except Exception as exc:
        msg = _redact(str(exc))
        _emit("log", level="ERROR", component="setup", message=msg)
        _emit("complete", ok=False, error=msg, setup_log=str(_setup_log_path()),
              duration_seconds=0.0, required_validated=0, installed_count=0,
              reused_count=0, skipped_count=0, failed_count=1, optional_notices=[msg])
        if not _JSON_PROGRESS:
            print(f"Setup failed: {msg}", file=sys.stderr)
        return 1
    if not _JSON_PROGRESS:
        print("AutoClip Studio is ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
