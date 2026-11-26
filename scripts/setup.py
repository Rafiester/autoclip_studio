"""Validate or provision the checkout's Windows runtime, media tools, and optional Whisper model."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
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

_JSON_PROGRESS = False


def _emit(event: str, **payload: object) -> None:
    if _JSON_PROGRESS:
        print(json.dumps({"event": event, **payload}, ensure_ascii=False), flush=True)


def run(*command: str | Path, capture: bool = False, timeout: int | None = None) -> str:
    result = subprocess.run(
        [str(item) for item in command], check=True, cwd=ROOT, text=True, encoding="utf-8", errors="replace",
        stdout=subprocess.PIPE if capture else None, stderr=subprocess.STDOUT if capture else None,
        stdin=subprocess.DEVNULL, timeout=timeout,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        env={**os.environ, "UV_PYTHON_INSTALL_DIR": str(ROOT / ".runtime"),
             "UV_CACHE_DIR": str(ROOT / ".uv-cache"), "PYTHONIOENCODING": "utf-8"},
    )
    return result.stdout or ""


def _verified(path: Path, digest: str) -> bool:
    if not path.is_file():
        return False
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest().lower() == digest.lower()


def download(url: str, target: Path, digest: str | None = None, *, component: str = "") -> None:
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
                        if component and (now - last_emit >= 0.15 or (total and downloaded >= total)):
                            elapsed = max(0.001, now - started)
                            speed = (downloaded - existing) / elapsed
                            eta = ((total - downloaded) / speed) if (total and speed > 0) else None
                            _emit("progress", id=component, state="Downloading",
                                  downloaded=downloaded, total=total, speed_bps=speed, eta_seconds=eta)
                            last_emit = now
            if digest and not _verified(part, digest):
                part.unlink(missing_ok=True)
                raise RuntimeError(f"SHA-256 verification failed for {target.name}.")
            part.replace(target)
            return
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt + 1 >= 3:
                raise
            time.sleep(2 ** attempt)


def _manifest_component(comp_id: str) -> dict:
    if MANIFEST.is_file():
        data = json.loads(MANIFEST.read_text(encoding="utf-8"))
        for item in data.get("components", []):
            if item.get("id") == comp_id:
                return item
    return {}


def ensure_uv() -> None:
    _emit("progress", id="uv", state="Checking")
    if UV.is_file():
        _emit("progress", id="uv", state="Reused existing")
        return
    if sys.platform == "darwin" and (found := shutil.which("uv")):
        _emit("progress", id="uv", state="Reused existing", detail=found)
        return
    tc = json.loads(TOOLCHAIN.read_text(encoding="utf-8"))
    pins = tc["uv_macos_arm64"] if sys.platform == "darwin" else tc["uv"]
    TOOLS.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=TOOLS, prefix="uv-") as directory:
        ext = ".tar.gz" if sys.platform == "darwin" else ".zip"
        archive = Path(directory) / f"uv{ext}"
        download(pins["url"], archive, pins.get("sha256") or None, component="uv")
        _emit("progress", id="uv", state="Extracting")
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
    _emit("progress", id="uv", state="Ready")


def media_tool_problems() -> list[str]:
    problems = []
    suffix = ".exe" if os.name == "nt" else ""
    for name in ("ffmpeg", "ffprobe"):
        executable = FFMPEG / "bin" / f"{name}{suffix}"
        if not executable.is_file() and os.name != "nt":
            for candidate in (Path("/opt/homebrew/bin") / name, Path("/usr/local/bin") / name):
                if candidate.is_file():
                    executable = candidate
                    break
        if executable.is_file():
            try:
                output = run(executable, "-version", capture=True, timeout=10)
                if not output.startswith(f"{name} version "):
                    problems.append(f"{name} did not report its version")
                continue
            except (OSError, subprocess.SubprocessError):
                problems.append(f"{name} is missing or could not start")
                continue
        if shutil.which(name):
            continue
        problems.append(f"{name} is missing or could not start")
    return problems


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


def check() -> list[str]:
    problems = []
    if not ((3, 11) <= sys.version_info[:2] < (3, 13)):
        problems.append("Python 3.11 or 3.12 is required")
    if not PYTHONW.is_file():
        problems.append("The project's Python interpreter is missing")
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        requirement, _, marker = line.partition(";")
        name, separator, expected = requirement.strip().partition("==")
        if not separator or not _marker_applies(marker):
            continue
        try:
            installed = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            problems.append(f"{name} is missing")
        else:
            if installed != expected:
                problems.append(f"{name} {expected} is required (installed {installed})")
    return problems + media_tool_problems()


def ensure_ffmpeg() -> None:
    _emit("progress", id="ffmpeg", state="Checking")
    if not media_tool_problems():
        _emit("progress", id="ffmpeg", state="Reused existing")
        return
    if sys.platform == "darwin":
        brew = shutil.which("brew") or ("/opt/homebrew/bin/brew" if Path("/opt/homebrew/bin/brew").is_file() else None)
        if brew:
            _emit("progress", id="ffmpeg", state="Installing", detail="Installing ffmpeg via Homebrew")
            run(brew, "install", "ffmpeg")
            if not media_tool_problems():
                _emit("progress", id="ffmpeg", state="Ready")
                return
        raise RuntimeError("FFmpeg is missing on macOS. Install via Homebrew (`brew install ffmpeg`) with VideoToolbox and libass support.")
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
            download(url, archive, digest, component="ffmpeg")
        except OSError:
            download(fallback, archive, digest, component="ffmpeg")
        _emit("progress", id="ffmpeg", state="Extracting")
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
    _emit("progress", id="ffmpeg", state="Ready")


def ensure_default_whisper_model(*, cloud_only: bool = False) -> None:
    model_id = "whisper_model_large_v3_turbo"
    _emit("progress", id=model_id, state="Checking")
    if cloud_only:
        _emit("progress", id=model_id, state="Ready", detail="Skipped (cloud-only mode)")
        return
    try:
        sys.path.insert(0, str(ROOT))
        from autoclip_studio.speech_engine import SpeechSettings, download_speech_model, find_executable, find_model
        settings = SpeechSettings()
        existing = find_model("large-v3-turbo", settings, find_executable(settings.executable))
        if existing:
            _emit("progress", id=model_id, state="Reused existing", detail=str(existing))
            return
        _emit("progress", id=model_id, state="Downloading")
        download_speech_model("large-v3-turbo", settings)
        _emit("progress", id=model_id, state="Ready")
    except Exception as error:
        _emit("progress", id=model_id, state="Failed", detail=str(error))


def ensure_runtime(*, tests: bool = False, local_whisper: bool = False, cloud_only: bool = False) -> None:
    ensure_uv()
    _emit("progress", id="python_runtime", state="Checking")
    try:
        usable = run(PYTHON, "-c", "import sys; print((3, 11) <= sys.version_info[:2] < (3, 13))",
                     capture=True, timeout=15).strip() == "True"
    except (OSError, subprocess.SubprocessError):
        usable = False
    if not usable or not PYTHONW.is_file():
        _emit("progress", id="python_runtime", state="Installing")
        _emit("progress", id="python_venv", state="Installing")
        if not _JSON_PROGRESS:
            print("Preparing the project-local Python environment...", flush=True)
        run(UV, "venv", "--allow-existing", "--managed-python", "--python", "3.12", ROOT / ".venv")
        _emit("progress", id="python_runtime", state="Ready")
        _emit("progress", id="python_venv", state="Ready")
    else:
        _emit("progress", id="python_runtime", state="Reused existing")
        _emit("progress", id="python_venv", state="Reused existing")

    _emit("progress", id="pyside6", state="Installing")
    _emit("progress", id="python_packages", state="Installing")
    if not _JSON_PROGRESS:
        print("Checking Python dependencies...", flush=True)
    run(UV, "pip", "install", "--python", PYTHON, "-r", REQUIREMENTS)
    _emit("progress", id="pyside6", state="Ready")
    _emit("progress", id="python_packages", state="Ready")
    _emit("progress", id="yt_dlp", state="Ready")
    _emit("progress", id="opencv_face_model", state="Ready")
    _emit("progress", id="whisper_engine", state="Ready")

    ensure_ffmpeg()

    if local_whisper or cloud_only:
        ensure_default_whisper_model(cloud_only=cloud_only)

    _emit("progress", id="verification", state="Verifying")
    run(PYTHON, __file__, "--check")
    _emit("progress", id="verification", state="Ready")
    if tests:
        run(PYTHON, "-m", "pytest")


def main() -> int:
    global _JSON_PROGRESS
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Validate the current environment without installing.")
    parser.add_argument("--json-progress", action="store_true", help="Emit structured JSON progress lines.")
    parser.add_argument("--local-whisper", action="store_true", help="Ensure default Whisper large-v3-turbo model.")
    parser.add_argument("--cloud-only", action="store_true", help="Skip downloading local Whisper models.")
    parser.add_argument("--no-tests", action="store_true", help="Skip running pytest after setup.")
    options = parser.parse_args()
    _JSON_PROGRESS = options.json_progress
    if options.check:
        problems = check()
        for problem in problems:
            print(problem, file=sys.stderr)
        return int(bool(problems))
    ensure_runtime(
        tests=(not options.no_tests and not options.json_progress),
        local_whisper=options.local_whisper,
        cloud_only=options.cloud_only,
    )
    if not _JSON_PROGRESS:
        print("AutoClip Studio is ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
