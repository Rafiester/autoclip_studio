"""Validate or provision the checkout's Windows runtime and media tools."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import uuid
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
UV = TOOLS / "uv.exe"
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
PYTHONW = PYTHON.with_name("pythonw.exe")
FFMPEG = TOOLS / "ffmpeg"
REQUIREMENTS = ROOT / "requirements.txt"
FFMPEG_RELEASE = "ffmpeg-9.1-essentials_build"
FFMPEG_SHA256 = "b9bbbb3acece000cbeec994f856649354e52c773807841e78908374b24d658fd"


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


def download(url: str, target: Path, digest: str | None = None) -> None:
    def verified(path: Path) -> bool:
        if not path.is_file():
            return False
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest() == digest

    if digest and verified(target):
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    pending = target.with_suffix(target.suffix + ".part")
    try:
        with urllib.request.urlopen(url, timeout=45) as response, pending.open("wb") as stream:
            shutil.copyfileobj(response, stream)
        if digest and not verified(pending):
            raise RuntimeError(f"SHA-256 verification failed for {target.name}.")
        pending.replace(target)
    finally:
        pending.unlink(missing_ok=True)


def ensure_uv() -> None:
    if UV.is_file():
        return
    pins = json.loads((ROOT / "scripts" / "toolchain.json").read_text(encoding="utf-8"))["uv"]
    TOOLS.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=TOOLS, prefix="uv-") as directory:
        archive = Path(directory) / "uv.zip"
        download(pins["url"], archive, pins["sha256"])
        with zipfile.ZipFile(archive) as payload:
            member = next(name for name in payload.namelist() if Path(name).name == "uv.exe")
            executable = Path(directory) / "uv.exe"
            with payload.open(member) as source, executable.open("wb") as target:
                shutil.copyfileobj(source, target)
        executable.replace(UV)


def media_tool_problems() -> list[str]:
    problems = []
    for name in ("ffmpeg", "ffprobe"):
        executable = FFMPEG / "bin" / f"{name}.exe"
        try:
            output = run(executable, "-version", capture=True, timeout=10)
            if not output.startswith(f"{name} version "):
                problems.append(f"{name} did not report its version")
        except (OSError, subprocess.SubprocessError):
            problems.append(f"{name} is missing or could not start")
    return problems


def check() -> list[str]:
    problems = []
    if not ((3, 11) <= sys.version_info[:2] < (3, 13)):
        problems.append("Python 3.11 or 3.12 is required")
    if not PYTHONW.is_file():
        problems.append("The project's windowed Python interpreter is missing")
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        requirement, _, marker = line.partition(";")
        name, separator, expected = requirement.strip().partition("==")
        if not separator or ('sys_platform == "win32"' in marker and sys.platform != "win32"):
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
    if not media_tool_problems():
        return
    print("Preparing FFmpeg and ffprobe...", flush=True)
    with tempfile.TemporaryDirectory(dir=TOOLS, prefix="ffmpeg-") as directory:
        folder = Path(directory)
        archive = folder / "ffmpeg.zip"
        try:
            download(f"https://github.com/GyanD/codexffmpeg/releases/download/9.1/{FFMPEG_RELEASE}.zip",
                     archive, FFMPEG_SHA256)
        except OSError:
            download(f"https://www.gyan.dev/ffmpeg/builds/packages/{FFMPEG_RELEASE}.zip", archive, FFMPEG_SHA256)
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


def ensure_runtime(*, tests: bool = False) -> None:
    ensure_uv()
    try:
        usable = run(PYTHON, "-c", "import sys; print((3, 11) <= sys.version_info[:2] < (3, 13))",
                     capture=True, timeout=15).strip() == "True"
    except (OSError, subprocess.SubprocessError):
        usable = False
    if not usable or not PYTHONW.is_file():
        print("Preparing the project-local Python environment...", flush=True)
        run(UV, "venv", "--allow-existing", "--managed-python", "--python", "3.12", ROOT / ".venv")
    print("Checking Python dependencies...", flush=True)
    run(UV, "pip", "install", "--python", PYTHON, "-r", REQUIREMENTS)
    ensure_ffmpeg()
    run(PYTHON, __file__, "--check")
    if tests:
        run(PYTHON, "-m", "pytest")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Validate the current environment without installing.")
    options = parser.parse_args()
    if options.check:
        problems = check()
        for problem in problems:
            print(problem, file=sys.stderr)
        return int(bool(problems))
    ensure_runtime(tests=True)
    print("AutoClip Studio is ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
