"""Centralized OS and CPU architecture detection and platform helpers."""
from __future__ import annotations

import os
import platform
import subprocess
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


@dataclass(frozen=True, slots=True)
class PlatformInfo:
    system: str          # "windows" | "macos" | "linux"
    machine: str         # "arm64" | "x64" | ...
    is_windows: bool
    is_macos: bool
    is_apple_silicon: bool
    cpu_brand: str
    is_rosetta: bool = False
    os_version: str = ""


def _is_rosetta_translated() -> bool:
    if sys.platform != "darwin":
        return False
    try:
        out = subprocess.run(
            ["sysctl", "-n", "sysctl.proc_translated"],
            capture_output=True, text=True, timeout=2,
        ).stdout.strip()
        return out == "1"
    except (OSError, subprocess.SubprocessError):
        return False


@lru_cache(maxsize=1)
def detect_platform() -> PlatformInfo:
    raw_sys = sys.platform.lower()
    if raw_sys.startswith("win"):
        system = "windows"
    elif raw_sys == "darwin":
        system = "macos"
    else:
        system = "linux"

    raw_arch = platform.machine().lower()
    if raw_arch in {"amd64", "x86_64", "x64"}:
        arch = "x64"
    elif raw_arch in {"arm64", "aarch64"}:
        arch = "arm64"
    else:
        arch = raw_arch or "unknown"

    is_windows = system == "windows"
    is_macos = system == "macos"
    is_rosetta = _is_rosetta_translated() if is_macos else False
    is_apple_silicon = is_macos and (arch == "arm64" or is_rosetta)
    os_version = platform.mac_ver()[0] if is_macos else platform.release()

    brand = platform.processor() or ""
    if is_macos:
        try:
            out = subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                capture_output=True, text=True, timeout=2,
            ).stdout.strip()
            if out:
                brand = out
        except (OSError, subprocess.SubprocessError):
            pass
    return PlatformInfo(
        system=system,
        machine=arch,
        is_windows=is_windows,
        is_macos=is_macos,
        is_apple_silicon=is_apple_silicon,
        cpu_brand=brand or arch,
        is_rosetta=is_rosetta,
        os_version=os_version,
    )


def subprocess_creation_flags(priority_flags: int = 0) -> int:
    if os.name == "nt":
        return subprocess.CREATE_NO_WINDOW | priority_flags
    return 0


def venv_python_path(root: Path, *, windowed: bool = False) -> Path:
    if os.name == "nt":
        name = "pythonw.exe" if windowed else "python.exe"
        return root / ".venv" / "Scripts" / name
    candidate = root / ".venv" / "bin" / "python3"
    return candidate if candidate.exists() else root / ".venv" / "bin" / "python"


def uv_executable_path(root: Path) -> Path:
    name = "uv.exe" if os.name == "nt" else "uv"
    return root / "tools" / name


def marker_matches(marker: str, *, plat: str | None = None, arch: str | None = None) -> bool:
    """Evaluate simple PEP 508 environment markers used in requirements files."""
    if not marker.strip():
        return True
    cur_plat = plat or sys.platform
    cur_arch = (arch or platform.machine()).lower()
    if 'sys_platform == "win32"' in marker and cur_plat != "win32":
        return False
    if 'sys_platform == "darwin"' in marker and cur_plat != "darwin":
        return False
    if 'platform_machine == "arm64"' in marker and cur_arch not in {"arm64", "aarch64"}:
        return False
    return True
