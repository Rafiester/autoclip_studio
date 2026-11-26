"""Cross-platform safe clip actions, Recycle Bin / Trash removal, and shared asset protection."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QFile, QUrl
from PySide6.QtGui import QDesktopServices

from .database import Database
from .diagnostics import LOG
from .paths import CACHE_DIR, DATA_ROOT, LOG_DIR

_DEFAULT_PROTECTED_ROOTS: tuple[Path, ...] = (CACHE_DIR, DATA_ROOT / "jobs", LOG_DIR)


@dataclass(slots=True)
class ClipDeletionReport:
    requested_count: int
    deleted_ids: list[str] = field(default_factory=list)
    deleted_file_count: int = 0
    cleaned_missing_count: int = 0
    failed_items: list[tuple[str, str]] = field(default_factory=list)  # (title, error_message)
    skipped_shared_assets: int = 0


def open_clip_file(path: Path) -> bool:
    """Open an existing generated clip with the OS default media player."""
    if not path.is_file():
        return False
    return bool(QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.resolve()))))


def reveal_in_file_manager(path: Path) -> bool:
    """Reveal a clip in Windows File Explorer or macOS Finder, falling back to its folder if missing."""
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path

    if resolved.is_file():
        if sys.platform == "win32":
            try:
                subprocess.Popen(["explorer.exe", f"/select,{resolved}"])
                return True
            except OSError:
                pass
        elif sys.platform == "darwin":
            try:
                subprocess.Popen(["open", "-R", str(resolved)])
                return True
            except OSError:
                pass
        return bool(QDesktopServices.openUrl(QUrl.fromLocalFile(str(resolved.parent))))

    parent = resolved.parent
    if parent.is_dir():
        if sys.platform == "darwin":
            try:
                subprocess.Popen(["open", str(parent)])
                return True
            except OSError:
                pass
        return bool(QDesktopServices.openUrl(QUrl.fromLocalFile(str(parent))))
    return False


def move_path_to_trash(path: Path) -> tuple[bool, str]:
    """Move a file to OS Recycle Bin / Trash cleanly, confirming removal before reporting success."""
    if not path.exists():
        return True, ""
    if path.is_dir():
        return False, f"Refusing to delete directory '{path.name}'."
    try:
        ok = bool(QFile.moveToTrash(str(path)))
    except Exception as exc:
        return False, str(exc)
    if ok and not path.exists():
        return True, ""

    if sys.platform.startswith("linux") and shutil.which("gio"):
        try:
            proc = subprocess.run(["gio", "trash", str(path)], capture_output=True, text=True, check=False)
            if proc.returncode == 0 and not path.exists():
                return True, ""
            if proc.stderr:
                return False, proc.stderr.strip()
        except OSError as exc:
            return False, str(exc)

    return False, f"Could not move '{path.name}' to Trash / Recycle Bin (it may be open in another app)."


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def is_protected_asset(
    candidate: Path,
    *,
    protected_refs: set[Path],
    protected_roots: tuple[Path, ...] = _DEFAULT_PROTECTED_ROOTS,
) -> bool:
    """Return True if candidate is a directory, shared cache/source, manifest, or referenced by another clip."""
    if not candidate or candidate.is_dir():
        return True
    if candidate.name.lower() == "project.json":
        return True
    try:
        resolved = candidate.resolve()
    except OSError:
        resolved = candidate
    if resolved in protected_refs:
        return True
    return any(_is_under(resolved, root) for root in protected_roots)


def _prune_manifest_clips(project_dir: Path, removed_clip_paths: set[Path]) -> None:
    """Remove deleted clip entries from project.json manifest without disturbing project metadata."""
    manifest_path = project_dir / "project.json"
    if not manifest_path.is_file():
        return
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    clips = data.get("clips")
    if not isinstance(clips, list):
        return
    filtered = []
    changed = False
    for entry in clips:
        raw_path = str(entry.get("path") or "") if isinstance(entry, dict) else ""
        if raw_path:
            try:
                if Path(raw_path).resolve() in removed_clip_paths:
                    changed = True
                    continue
            except OSError:
                pass
        filtered.append(entry)
    if not changed:
        return
    data["clips"] = filtered
    tmp_path = project_dir / "project.json.tmp"
    try:
        tmp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp_path.replace(manifest_path)
    except OSError as exc:
        LOG.warning("Failed to update manifest %s after clip removal: %s", manifest_path, exc)
    finally:
        tmp_path.unlink(missing_ok=True)


def delete_clips_transaction(
    clips: list[dict],
    database: Database,
    *,
    delete_files: bool = False,
    trash_fn: Callable[[Path], tuple[bool, str]] | None = None,
    release_handles: Callable[[set[Path]], None] | None = None,
    protected_roots: tuple[Path, ...] = _DEFAULT_PROTECTED_ROOTS,
) -> ClipDeletionReport:
    """Safely remove clips from library and optionally move clip-specific files to Trash."""
    report = ClipDeletionReport(requested_count=len(clips))
    if not clips:
        return report

    trash = trash_fn or move_path_to_trash
    target_ids = {str(c["id"]) for c in clips if c.get("id")}

    if not delete_files:
        removed = database.delete_clips(list(target_ids))
        report.deleted_ids = [str(r["id"]) for r in removed]
        report.cleaned_missing_count = sum(1 for r in removed if not Path(str(r.get("path") or "")).is_file())
        return report

    # Release open player handles on Windows/macOS before touching files.
    if release_handles is not None:
        release_paths: set[Path] = set()
        for c in clips:
            raw = str(c.get("path") or "").strip()
            if raw:
                try:
                    release_paths.add(Path(raw).resolve())
                except OSError:
                    pass
        if release_paths:
            release_handles(release_paths)

    protected_refs = database.other_clips_asset_refs(target_ids)
    # Also protect thumbnails/files belonging to any clip whose media deletion later fails.
    staged_success: list[dict] = []
    manifest_updates: dict[Path, set[Path]] = {}

    for clip in clips:
        cid = str(clip.get("id") or "")
        title = str(clip.get("title") or cid or "Clip")
        clip_path = Path(str(clip.get("path") or ""))
        had_file = clip_path.is_file()

        if had_file:
            if is_protected_asset(clip_path, protected_refs=protected_refs, protected_roots=protected_roots):
                report.failed_items.append((title, f"'{clip_path.name}' is protected (source or shared reference) and cannot be deleted."))
                continue
            ok, err = trash(clip_path)
            if not ok or clip_path.exists():
                report.failed_items.append((title, err or f"Could not remove '{clip_path.name}'."))
                continue
            report.deleted_file_count += 1
        else:
            report.cleaned_missing_count += 1

        staged_success.append(clip)
        if clip_path.parent.is_dir():
            try:
                manifest_updates.setdefault(clip_path.parent, set()).add(clip_path.resolve())
            except OSError:
                pass

    # Recompute protected refs excluding ONLY the clips whose primary deletion actually succeeded.
    succeeded_ids = [str(c["id"]) for c in staged_success]
    remaining_refs = database.other_clips_asset_refs(set(succeeded_ids))
    for clip in staged_success:
        for thumb_col in ("thumbnail_path", "thumbnail_raw_path"):
            raw_t = str(clip.get(thumb_col) or "").strip()
            if not raw_t:
                continue
            t_path = Path(raw_t)
            if not t_path.is_file():
                continue
            if is_protected_asset(t_path, protected_refs=remaining_refs, protected_roots=protected_roots):
                report.skipped_shared_assets += 1
                continue
            trash(t_path)

    if succeeded_ids:
        database.delete_clips(succeeded_ids)
        report.deleted_ids = succeeded_ids
        for proj_dir, paths_set in manifest_updates.items():
            _prune_manifest_clips(proj_dir, paths_set)

    return report
