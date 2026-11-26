"""Automatic project naming, filesystem-safe folder sanitization, and collision-free output directory allocation."""
from __future__ import annotations

import json
import re
import threading
from collections.abc import Iterable
from pathlib import Path

from .media import is_youtube_url, youtube_id

_DIR_ALLOC_LOCK = threading.Lock()

_WINDOWS_RESERVED_NAMES = frozenset({
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
})


def derive_project_name(
    source: str,
    *,
    youtube_title: str = "",
    local_name: str = "",
    fallback: str = "Untitled Project",
) -> str:
    """Return a clean human-friendly project name automatically derived from source & metadata."""
    cleaned_source = str(source or "").strip()
    if is_youtube_url(cleaned_source):
        title = str(youtube_title or "").strip()
        if title:
            return title
        vid = youtube_id(cleaned_source)
        return f"YouTube Video ({vid})" if vid else "YouTube Video"

    cleaned_local = str(local_name or "").strip()
    if cleaned_local:
        return cleaned_local

    if cleaned_source:
        stem = Path(cleaned_source).stem.strip()
        if stem:
            return stem
        name = Path(cleaned_source).name.strip()
        if name:
            return name
        return "Local Video"

    return fallback


def sanitize_project_dir_name(name: str, *, fallback: str = "project", max_len: int = 64) -> str:
    """Convert any video title or filename into a valid Windows/macOS directory name."""
    raw = str(name or "").strip()
    # Replace reserved/invalid filesystem characters and control chars
    sanitized = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "-", raw)
    # Collapse whitespace runs into single spaces and strip unsafe trailing/leading punctuation
    sanitized = re.sub(r"\s+", " ", sanitized).strip(" .-_")
    sanitized = re.sub(r"-{2,}", "-", sanitized)

    if len(sanitized) > max_len:
        sanitized = sanitized[:max_len].rstrip(" .-_")

    if not sanitized:
        sanitized = fallback

    stem_upper = sanitized.split(".", 1)[0].strip().upper()
    if stem_upper in _WINDOWS_RESERVED_NAMES:
        sanitized = f"{sanitized}-project"
    return sanitized


def _manifest_matches_project(folder: Path, project_id: str) -> bool:
    manifest = folder / "project.json"
    if not manifest.is_file():
        return False
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        return isinstance(payload, dict) and str(payload.get("project_id") or "") == str(project_id)
    except (OSError, ValueError, TypeError):
        return False


def allocate_project_output_dir(output_root: Path, project_name: str, project_id: str) -> Path:
    """Create or reuse a collision-free, filesystem-safe output folder for this project."""
    output_root.mkdir(parents=True, exist_ok=True)
    with _DIR_ALLOC_LOCK:
        # Preserve legacy <output_root>/<project_id> if already present
        legacy_dir = output_root / project_id
        if legacy_dir.is_dir():
            return legacy_dir

        # Preserve prior folder if a resumed project already wrote project.json here
        try:
            for entry in output_root.iterdir():
                if entry.is_dir() and _manifest_matches_project(entry, project_id):
                    return entry
        except OSError:
            pass

        base = sanitize_project_dir_name(project_name)
        candidates: list[str] = [base, *(f"{base} ({idx})" for idx in range(2, 100))]
        if project_id:
            candidates.append(f"{base[:48].rstrip(' .-_') or 'project'}-{project_id[:8]}")

        for dirname in candidates:
            target = output_root / dirname
            try:
                target.mkdir(parents=True, exist_ok=False)
                return target
            except FileExistsError:
                if _manifest_matches_project(target, project_id):
                    return target
                continue

        fallback = output_root / project_id
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def resolve_existing_project_output_dir(
    output_root: Path,
    project_id: str,
    *,
    clip_paths: Iterable[str | Path] = (),
) -> Path | None:
    """Locate a project's existing export folder (supports both slugged and legacy UUID folders)."""
    for raw_path in clip_paths:
        if raw_path:
            parent = Path(str(raw_path)).parent
            if parent.is_dir():
                return parent

    if output_root.is_dir():
        legacy = output_root / str(project_id)
        if legacy.is_dir():
            return legacy
        try:
            for entry in output_root.iterdir():
                if entry.is_dir() and _manifest_matches_project(entry, project_id):
                    return entry
        except OSError:
            pass
    return None
