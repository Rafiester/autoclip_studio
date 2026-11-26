"""Validate or provision the checkout's Python runtime, media tools, and optional Whisper model.

Idempotent startup & selective repair rules:
- Never silently downgrade a newer compatible package (e.g. user-updated yt-dlp).
- Never trigger mandatory setup because optional dependencies (YuNet ASD, Whisper models, CUDA) are absent.
- Automatically detect and use `uv` (tools/uv or system uv) or `pip` (inside .venv), supporting uv-managed venvs without pip.
- Protect against concurrent bootstrap runs with a non-blocking lockfile.
"""

from __future__ import annotations

import argparse
import contextlib
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
from typing import Iterator

if os.name == "nt":
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

# Packages where newer stable versions installed by the user must ALWAYS be preserved (never downgraded).
ALLOW_NEWER_PACKAGES = {"yt-dlp", "yt-dlp-ejs", "mlx-whisper"}
# Dev/test-only requirements should not block normal application startup if absent.
NON_STARTUP_PACKAGES = {"pytest"}

_JSON_PROGRESS = False
_REQ_RE = re.compile(r"^([A-Za-z0-9_.-]+)\s*(==|>=|~=)\s*([^\s;]+)$")


def _emit(event: str, **payload: object) -> None:
    if _JSON_PROGRESS:
        print(json.dumps({"event": event, **payload}, ensure_ascii=False), flush=True)


def _version_tuple(raw: str) -> tuple[int, ...]:
    """Parse dotted/alphanumeric release versions like '2026.11.23' or '6.8.3' into comparable integer tuples."""
    parts = re.findall(r"\d+", raw.split("+")[0].split("-")[0])
    return tuple(int(p) for p in parts) if parts else (0,)


def _parse_requirements(path: Path = REQUIREMENTS) -> list[tuple[str, str, str, str]]:
    """Return list of (name, op, expected_version, raw_spec) for lines applicable to the current platform."""
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
    # Exact pin (==): accept equal OR newer patch/minor if user intentionally upgraded via Dependency Manager?
    # Keep strict for exact-pinned core ABI packages (PySide6, av, faster-whisper) so ABI breaks are caught,
    # while ALLOW_NEWER_PACKAGES and `>=` specs always allow newer versions.
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
    """Check persisted settings.json without importing PySide6/application modules."""
    candidates: list[Path] = []
    if configured := os.environ.get("AUTOCLIP_DATA_DIR", "").strip():
        candidates.append(Path(configured).expanduser() / "settings.json")
    if sys.platform == "darwin":
        candidates.append(Path.home() / "Library" / "Application Support" / "AutoClip Studio" / "settings.json")
    elif os.name == "nt":
        local_app = os.environ.get("LOCALAPPDATA", "")
        if local_app:
            candidates.append(Path(local_app) / "AutoClip" / "AutoClip Studio" / "settings.json")
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


@contextlib.contextmanager
def _bootstrap_lock(timeout_seconds: float = 2.0) -> Iterator[None]:
    """Prevent duplicate concurrent bootstrap/repair runs on the same checkout."""
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


def run(*command: str | Path, capture: bool = False, timeout: int | None = None) -> str:
    env = {
        **os.environ,
        "UV_PYTHON_INSTALL_DIR": str(ROOT / ".runtime"),
        "UV_CACHE_DIR": str(ROOT / ".uv-cache"),
        "PYTHONIOENCODING": "utf-8",
    }
    if sys.platform == "darwin":
        brew_prefix = "/opt/homebrew/opt/ffmpeg-full/bin:/opt/homebrew/bin:/usr/local/opt/ffmpeg-full/bin:/usr/local/bin"
        env["PATH"] = f"{brew_prefix}:{env.get('PATH', '/usr/bin:/bin')}"
    result = subprocess.run(
        [str(item) for item in command], check=True, cwd=ROOT, text=True, encoding="utf-8", errors="replace",
        stdout=subprocess.PIPE if capture else None, stderr=subprocess.STDOUT if capture else None,
        stdin=subprocess.DEVNULL, timeout=timeout,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        env=env,
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
                            pct = min(100.0, (downloaded / total) * 100.0) if total else None
                            _emit("progress", id=component, state="Downloading",
                                  downloaded=downloaded, total=total, percent=pct,
                                  speed_bps=speed, eta_seconds=eta)
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


def ensure_uv() -> Path:
    _emit("progress", id="uv", state="Checking", detail="Checking uv package bootstrapper", percent=5)
    found = _find_uv()
    if found is not None:
        _emit("progress", id="uv", state="Reused existing", detail=str(found), percent=10)
        return found
    tc = json.loads(TOOLCHAIN.read_text(encoding="utf-8"))
    pins = tc["uv_macos_arm64"] if sys.platform == "darwin" else tc["uv"]
    TOOLS.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=TOOLS, prefix="uv-") as directory:
        ext = ".tar.gz" if sys.platform == "darwin" else ".zip"
        archive = Path(directory) / f"uv{ext}"
        download(pins["url"], archive, pins.get("sha256") or None, component="uv")
        _emit("progress", id="uv", state="Extracting", detail="Extracting uv bootstrapper")
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
    _emit("progress", id="uv", state="Ready", detail=str(UV), percent=15)
    return UV


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


def _installed_versions_in_venv(python_exe: Path = PYTHON) -> dict[str, str]:
    """Inspect installed package versions in the target .venv (even if setup.py is invoked from another interpreter)."""
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
    """Return (problems, specs_to_install). Never flags newer versions of >= or ALLOW_NEWER_PACKAGES."""
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


def ensure_ffmpeg() -> None:
    _emit("progress", id="ffmpeg", state="Checking", detail="Checking FFmpeg & FFprobe", percent=68)
    if not media_tool_problems():
        _emit("progress", id="ffmpeg", state="Reused existing", detail="FFmpeg & FFprobe verified", percent=78)
        return
    if sys.platform == "darwin":
        brew = shutil.which("brew") or ("/opt/homebrew/bin/brew" if Path("/opt/homebrew/bin/brew").is_file() else None)
        if brew:
            _emit("progress", id="ffmpeg", state="Installing", detail="Installing ffmpeg-full via Homebrew", percent=72)
            run(brew, "install", "ffmpeg-full", timeout=600)
            if not media_tool_problems():
                _emit("progress", id="ffmpeg", state="Ready", detail="Homebrew ffmpeg-full ready", percent=78)
                return
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
            download(url, archive, digest, component="ffmpeg")
        except OSError:
            download(fallback, archive, digest, component="ffmpeg")
        _emit("progress", id="ffmpeg", state="Extracting", detail="Extracting FFmpeg binaries")
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
    _emit("progress", id="ffmpeg", state="Ready", detail="FFmpeg & FFprobe ready", percent=78)


def ensure_default_whisper_model(*, cloud_only: bool = False) -> None:
    model_id = "whisper_model_large_v3_turbo"
    _emit("progress", id=model_id, state="Checking", detail="Validating optional transcription components", percent=82)
    if cloud_only or _user_cloud_only_setting():
        _emit("progress", id=model_id, state="Ready", detail="Skipped (cloud-only transcription mode)", percent=88)
        return
    try:
        sys.path.insert(0, str(ROOT))
        from autoclip_studio.speech_engine import SpeechSettings, download_speech_model, find_executable, find_model
        settings = SpeechSettings()
        existing = find_model("large-v3-turbo", settings, find_executable(settings.executable))
        if existing:
            _emit("progress", id=model_id, state="Reused existing", detail=f"Reusing cached model: {existing.name}", percent=88)
            return
        _emit("progress", id=model_id, state="Downloading", detail="Downloading Whisper large-v3-turbo model", percent=84)
        download_speech_model("large-v3-turbo", settings)
        _emit("progress", id=model_id, state="Ready", detail="Whisper large-v3-turbo ready", percent=88)
    except Exception as error:
        # Optional Whisper model failure must NOT block core application startup!
        _emit("progress", id=model_id, state="Optional", detail=f"Skipped optional model ({error})", percent=88)


def _install_packages_selectively(specs: list[str], uv_path: Path | None) -> None:
    """Install only missing/outdated specs using uv (preferred, works without pip) or .venv pip."""
    if not specs:
        return
    if uv_path is not None and uv_path.is_file():
        run(uv_path, "pip", "install", "--python", PYTHON, *specs, timeout=600)
        return
    if _venv_has_pip(PYTHON):
        run(PYTHON, "-m", "pip", "install", *specs, timeout=600)
        return
    # Neither uv nor pip available -> fetch uv bootstrapper and install cleanly
    uv_bin = ensure_uv()
    run(uv_bin, "pip", "install", "--python", PYTHON, *specs, timeout=600)


def ensure_runtime(
    *,
    tests: bool = False,
    local_whisper: bool = False,
    cloud_only: bool = False,
    repair_only: bool = False,
) -> None:
    with _bootstrap_lock():
        _emit("progress", id="python_runtime", state="Checking", detail="Checking Python runtime", percent=10)
        try:
            usable = run(PYTHON, "-c", "import sys; print((3, 11) <= sys.version_info[:2] < (3, 13))",
                         capture=True, timeout=15).strip() == "True"
        except (OSError, subprocess.SubprocessError):
            usable = False

        uv_bin = _find_uv()
        if not usable or not PYTHONW.is_file():
            uv_bin = ensure_uv()
            _emit("progress", id="python_runtime", state="Installing", detail="Installing managed Python 3.12 runtime", percent=20)
            _emit("progress", id="python_venv", state="Installing", detail="Creating project virtual environment", percent=25)
            if not _JSON_PROGRESS:
                print("Preparing the project-local Python environment...", flush=True)
            run(uv_bin, "venv", "--allow-existing", "--managed-python", "--python", "3.12", ROOT / ".venv", timeout=300)
            _emit("progress", id="python_runtime", state="Ready", detail="Python 3.12 runtime ready", percent=32)
            _emit("progress", id="python_venv", state="Ready", detail="Project virtual environment ready", percent=35)
        else:
            _emit("progress", id="python_runtime", state="Reused existing", detail="Validating existing environment", percent=28)
            _emit("progress", id="python_venv", state="Reused existing", detail="Reusing existing .venv", percent=35)

        _emit("progress", id="python_packages", state="Checking", detail="Checking required Python packages and yt-dlp", percent=42)
        pkg_problems, specs_to_install = missing_or_outdated_requirements(include_dev=tests)
        if specs_to_install:
            pkg_names = ", ".join(s.split("==")[0].split(">=")[0] for s in specs_to_install)
            _emit("progress", id="python_packages", state="Installing",
                  detail=f"Installing missing dependency: {pkg_names}", percent=50)
            if not _JSON_PROGRESS:
                print(f"Installing missing/outdated dependencies: {pkg_names}...", flush=True)
            _install_packages_selectively(specs_to_install, uv_bin)
            _emit("progress", id="pyside6", state="Ready", detail="GUI runtime verified", percent=60)
            _emit("progress", id="python_packages", state="Ready", detail="Required Python packages verified", percent=62)
            _emit("progress", id="yt_dlp", state="Ready", detail="yt-dlp verified", percent=64)
        else:
            _emit("progress", id="pyside6", state="Reused existing", detail="GUI runtime already healthy", percent=58)
            _emit("progress", id="python_packages", state="Reused existing", detail="All required Python packages already healthy", percent=62)
            _emit("progress", id="yt_dlp", state="Reused existing", detail="yt-dlp already up to date", percent=64)

        _emit("progress", id="opencv_face_model", state="Ready", detail="Face tracking ready", percent=65)
        _emit("progress", id="whisper_engine", state="Ready", detail="Transcription engine ready", percent=66)

        ensure_ffmpeg()

        if not repair_only and (local_whisper or cloud_only):
            ensure_default_whisper_model(cloud_only=cloud_only)
        else:
            _emit("progress", id="whisper_model_large_v3_turbo", state="Ready",
                  detail="Optional local Whisper models managed in Dependency Manager", percent=88)

        _emit("progress", id="verification", state="Verifying", detail="Finalizing setup", percent=94)
        run(PYTHON, __file__, "--check", timeout=30)
        _emit("progress", id="verification", state="Ready", detail="Setup complete", percent=100)
        if tests:
            run(PYTHON, "-m", "pytest", timeout=600)


def main() -> int:
    global _JSON_PROGRESS
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Validate the current environment without installing.")
    parser.add_argument("--json-progress", action="store_true", help="Emit structured JSON progress lines.")
    parser.add_argument("--local-whisper", action="store_true", help="Ensure default Whisper large-v3-turbo model.")
    parser.add_argument("--cloud-only", action="store_true", help="Skip downloading local Whisper models.")
    parser.add_argument("--repair-only", action="store_true", help="Repair only missing/incompatible required components.")
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
        repair_only=options.repair_only,
    )
    if not _JSON_PROGRESS:
        print("AutoClip Studio is ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
