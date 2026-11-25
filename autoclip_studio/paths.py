from __future__ import annotations

import os
from pathlib import Path

from platformdirs import user_data_dir


APP_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_ROOT = Path(__file__).resolve().parent
ASSETS_DIR = PACKAGE_ROOT / "assets"
DATA_ROOT = Path(os.environ.get("AUTOCLIP_DATA_DIR", user_data_dir("AutoClip Studio", "AutoClip")))
CACHE_DIR = DATA_ROOT / "cache"
OUTPUT_DIR = DATA_ROOT / "output"
LOG_DIR = DATA_ROOT / "logs"
MODEL_CACHE = CACHE_DIR / "models"
DB_PATH = DATA_ROOT / "autoclip.db"
SETTINGS_PATH = DATA_ROOT / "settings.json"


def asset_path(name: str) -> Path:
    """Resolve a bundled brand or UI asset path independent of working directory."""
    for base in (ASSETS_DIR, APP_ROOT / "assets", APP_ROOT):
        candidate = base / name
        if candidate.is_file():
            return candidate
    return ASSETS_DIR / name


def ensure_directories() -> None:
    for directory in (DATA_ROOT, CACHE_DIR, OUTPUT_DIR, LOG_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def bundled_binary(name: str) -> Path | None:
    suffix = ".exe" if os.name == "nt" else ""
    candidates = [
        APP_ROOT / "tools" / "ffmpeg" / "bin" / f"{name}{suffix}",
        APP_ROOT / "tools" / "ffmpeg" / f"{name}{suffix}",
        APP_ROOT / "tools" / "package" / f"{name}{suffix}",
    ]
    return next((path for path in candidates if path.exists()), None)

