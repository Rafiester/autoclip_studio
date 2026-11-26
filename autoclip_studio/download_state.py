"""Persistent download workspaces, partial-file verification, and rate-limit coordination."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import shutil
import threading
import time
from typing import Any, Callable, Iterator, TypeVar

from .paths import CACHE_DIR

LOG = logging.getLogger(__name__)
T = TypeVar("T")

DEFAULT_AUTH_INSTRUCTIONS = (
    "YouTube requested verification or sign-in before allowing further downloads. "
    "Automatic retries have been paused to avoid triggering stricter limits. "
    "Wait a few minutes, verify your YouTube session in your browser (or switch to a local video file), "
    "then click Retry After Verification."
)

# Conservative bounded backoff schedule (seconds) when HTTP 429 has no Retry-After header.
RATE_LIMIT_BACKOFF_SECONDS = (60.0, 180.0, 300.0, 600.0)
MIN_COOLDOWN_SECONDS = 15.0
MAX_COOLDOWN_SECONDS = 900.0

_PARTIAL_SUFFIXES = (".part", ".ytdl", ".mp4", ".m4a", ".webm", ".mkv", ".ts", ".f", ".temp")


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def format_preserved_bytes(num_bytes: int | None) -> str:
    """Format preserved partial download bytes cleanly for UI status text."""
    if not num_bytes or num_bytes <= 0:
        return "0 B"
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1000.0 or unit == "GB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1000.0
    return f"{num_bytes} B"


def format_cooldown(remaining_seconds: float | None) -> str:
    """Format remaining rate-limit cooldown as MM:SS or H:MM:SS."""
    if remaining_seconds is None or remaining_seconds <= 0:
        return "00:00"
    total = int(round(remaining_seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def workspaces_root(base_dir: Path | None = None) -> Path:
    root = (base_dir or CACHE_DIR) / "downloads" / "workspaces"
    root.mkdir(parents=True, exist_ok=True)
    return root


def workspace_key_for(
    source_url: str,
    *,
    video_id: str | None = None,
    project_id: str | None = None,
    cache_enabled: bool = True,
) -> str:
    """Return a deterministic, stable workspace directory name for a download."""
    if cache_enabled:
        if video_id:
            safe_vid = "".join(ch for ch in video_id if ch.isalnum() or ch in ("-", "_"))[:32]
            if safe_vid:
                return f"yt-{safe_vid}"
        digest = hashlib.sha256(source_url.encode("utf-8", errors="ignore")).hexdigest()[:16]
        return f"yt-{digest}"
    if project_id:
        safe_pid = "".join(ch for ch in project_id if ch.isalnum() or ch in ("-", "_"))[:48]
        if safe_pid:
            return f"job-{safe_pid}"
    digest = hashlib.sha256(source_url.encode("utf-8", errors="ignore")).hexdigest()[:16]
    return f"job-{digest}"


def workspace_path_for(
    source_url: str,
    *,
    video_id: str | None = None,
    project_id: str | None = None,
    cache_enabled: bool = True,
    base_dir: Path | None = None,
) -> Path:
    key = workspace_key_for(
        source_url, video_id=video_id, project_id=project_id, cache_enabled=cache_enabled,
    )
    path = workspaces_root(base_dir) / key
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass
class DownloadState:
    """Persistent download resume and recovery metadata stored in workspace state.json and SQLite."""

    workspace_key: str = ""
    workspace_dir: str = ""
    project_id: str = ""
    video_id: str = ""
    source_url: str = ""
    selected_format: str = ""
    status: str = "idle"  # downloading | paused | rate_limited | auth_required | failed | completed | cancelled
    downloaded_bytes: int = 0
    total_bytes: int | None = None
    partial_files: list[str] = field(default_factory=list)
    retry_after_epoch: float | None = None
    cooldown_seconds: float | None = None
    error_category: str = ""
    error_summary: str = ""
    auth_instructions: str = ""
    updated_at: str = field(default_factory=_utc_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> DownloadState | None:
        if not isinstance(raw, dict) or not raw:
            return None
        valid = {k: v for k, v in raw.items() if k in cls.__dataclass_fields__}
        if not valid:
            return None
        if "partial_files" in valid and not isinstance(valid["partial_files"], list):
            valid["partial_files"] = []
        return cls(**valid)

    def remaining_cooldown(self, now: float | None = None) -> float:
        if self.retry_after_epoch is None:
            return 0.0
        current = time.time() if now is None else now
        return max(0.0, float(self.retry_after_epoch) - current)


def state_file_path(workspace: Path) -> Path:
    return workspace / "state.json"


def load_download_state(workspace: Path) -> DownloadState | None:
    path = state_file_path(workspace)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return DownloadState.from_dict(data)
    except (OSError, ValueError, TypeError):
        return None


def save_download_state(workspace: Path, state: DownloadState) -> Path:
    workspace.mkdir(parents=True, exist_ok=True)
    state.workspace_dir = str(workspace)
    state.updated_at = _utc_iso()
    target = state_file_path(workspace)
    tmp = workspace / "state.json.tmp"
    try:
        tmp.write_text(json.dumps(state.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(target)
    finally:
        tmp.unlink(missing_ok=True)
    return target


def inspect_partial_files(workspace: Path) -> tuple[int, list[str]]:
    """Return (total_bytes_on_disk, sorted_partial_filenames) inside a download workspace."""
    if not workspace.is_dir():
        return 0, []
    total = 0
    names: list[str] = []
    for entry in sorted(workspace.iterdir(), key=lambda p: p.name):
        if not entry.is_file():
            continue
        if entry.name in ("state.json", "state.json.tmp", "receipt.txt"):
            continue
        lower = entry.name.lower()
        if any(lower.endswith(sfx) for sfx in _PARTIAL_SUFFIXES) or ".part" in lower:
            try:
                size = entry.stat().st_size
            except OSError:
                continue
            if size > 0:
                total += size
                names.append(entry.name)
    return total, names


def clean_partial_media_files(workspace: Path) -> int:
    """Remove partial/media files inside a workspace while preserving the workspace folder."""
    if not workspace.is_dir():
        return 0
    removed = 0
    for entry in list(workspace.iterdir()):
        if not entry.is_file():
            continue
        if entry.name in ("state.json", "state.json.tmp"):
            continue
        try:
            entry.unlink(missing_ok=True)
            removed += 1
        except OSError:
            pass
    return removed


def verify_workspace_identity(
    workspace: Path,
    *,
    video_id: str,
    source_url: str,
    selected_format: str,
) -> tuple[bool, str, int]:
    """Verify whether preserved partials in `workspace` belong to the same video and format.

    If a previous state belongs to a different video_id or selected_format, cleans mismatched
    partial media files so yt-dlp never appends incompatible streams together.
    Returns `(reused_partials, reason, preserved_bytes)`.
    """
    existing = load_download_state(workspace)
    partial_bytes, partial_files = inspect_partial_files(workspace)
    if not partial_files or partial_bytes <= 0:
        return False, "no_partials", 0
    if existing is None:
        return True, "untracked_partials_preserved", partial_bytes
    if video_id and existing.video_id and existing.video_id != video_id:
        LOG.info(
            "Workspace %s video_id changed (%s -> %s); clearing mismatched partials.",
            workspace.name, existing.video_id, video_id,
        )
        clean_partial_media_files(workspace)
        return False, "video_id_mismatch", 0
    if not video_id and existing.source_url and existing.source_url != source_url:
        clean_partial_media_files(workspace)
        return False, "source_url_mismatch", 0
    if selected_format and existing.selected_format and existing.selected_format != selected_format:
        LOG.info(
            "Workspace %s format changed (%s -> %s); clearing incompatible partials.",
            workspace.name, existing.selected_format, selected_format,
        )
        clean_partial_media_files(workspace)
        return False, "format_mismatch", 0
    return True, "verified_compatible", partial_bytes


def cleanup_workspace(workspace: Path | str | None) -> bool:
    """Remove a download workspace directory cleanly (used on completion or explicit cancel)."""
    if not workspace:
        return False
    path = Path(workspace)
    if not path.exists():
        return False
    try:
        shutil.rmtree(path, ignore_errors=False)
        return True
    except OSError:
        shutil.rmtree(path, ignore_errors=True)
        return not path.exists()


class DownloadConflictError(RuntimeError):
    """Raised when a duplicate download is requested while the same job or video is already active."""


class _InFlightRequest:
    def __init__(self) -> None:
        self.event = threading.Event()
        self.result: Any = None
        self.error: BaseException | None = None


class DownloadCoordinator:
    """Coordinates single-flight downloads, extraction deduplication, and 429/auth gates."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._active_projects: set[str] = set()
        self._active_videos: set[str] = set()
        self._in_flight_extractions: dict[str, _InFlightRequest] = {}
        self._global_cooldown_until: float = 0.0
        self._video_cooldown_until: dict[str, float] = {}
        self._consecutive_429: int = 0
        self._auth_suspended_reason: str = ""
        self._auth_suspended_videos: dict[str, str] = {}

    def reset_for_tests(self) -> None:
        with self._lock:
            self._active_projects.clear()
            self._active_videos.clear()
            self._in_flight_extractions.clear()
            self._global_cooldown_until = 0.0
            self._video_cooldown_until.clear()
            self._consecutive_429 = 0
            self._auth_suspended_reason = ""
            self._auth_suspended_videos.clear()

    def is_download_active(self, *, project_id: str = "", video_id: str = "") -> bool:
        with self._lock:
            if project_id and project_id in self._active_projects:
                return True
            if video_id and video_id in self._active_videos:
                return True
            return False

    @contextmanager
    def acquire_download_slot(self, *, project_id: str = "", video_id: str = "") -> Iterator[None]:
        with self._lock:
            if project_id and project_id in self._active_projects:
                raise DownloadConflictError(f"Download for job {project_id} is already in progress.")
            if video_id and video_id in self._active_videos:
                raise DownloadConflictError(f"Video {video_id} is already being downloaded by another task.")
            if project_id:
                self._active_projects.add(project_id)
            if video_id:
                self._active_videos.add(video_id)
        try:
            yield
        finally:
            with self._lock:
                if project_id:
                    self._active_projects.discard(project_id)
                if video_id:
                    self._active_videos.discard(video_id)

    def deduplicate_extraction(self, key: str, loader: Callable[[], T]) -> T:
        """Coalesce simultaneous extraction requests for the same key into a single execution."""
        with self._lock:
            existing = self._in_flight_extractions.get(key)
            if existing is None:
                flight = _InFlightRequest()
                self._in_flight_extractions[key] = flight
                is_owner = True
            else:
                flight = existing
                is_owner = False

        if not is_owner:
            flight.event.wait()
            if flight.error is not None:
                raise flight.error
            return flight.result

        try:
            result = loader()
            flight.result = result
            return result
        except BaseException as exc:
            flight.error = exc
            raise
        finally:
            with self._lock:
                if self._in_flight_extractions.get(key) is flight:
                    self._in_flight_extractions.pop(key, None)
            flight.event.set()

    def record_success(self, video_id: str = "") -> None:
        with self._lock:
            self._consecutive_429 = 0
            if video_id:
                self._video_cooldown_until.pop(video_id, None)
                self._auth_suspended_videos.pop(video_id, None)
            self._auth_suspended_reason = ""

    def record_rate_limit(
        self,
        video_id: str = "",
        *,
        retry_after_seconds: float | None = None,
        now: float | None = None,
    ) -> tuple[float, float]:
        """Record an HTTP 429 rate limit and return `(retry_after_epoch, cooldown_seconds)`."""
        current = time.time() if now is None else now
        with self._lock:
            self._consecutive_429 += 1
            if retry_after_seconds is not None and retry_after_seconds > 0:
                cooldown = max(MIN_COOLDOWN_SECONDS, min(MAX_COOLDOWN_SECONDS, float(retry_after_seconds)))
            else:
                idx = min(self._consecutive_429 - 1, len(RATE_LIMIT_BACKOFF_SECONDS) - 1)
                cooldown = RATE_LIMIT_BACKOFF_SECONDS[idx]
            until = current + cooldown
            self._global_cooldown_until = max(self._global_cooldown_until, until)
            if video_id:
                self._video_cooldown_until[video_id] = max(self._video_cooldown_until.get(video_id, 0.0), until)
            return until, cooldown

    def restore_rate_limit(self, video_id: str, retry_after_epoch: float) -> None:
        """Restore a persisted cooldown timestamp after application restart."""
        if retry_after_epoch <= time.time():
            return
        with self._lock:
            self._global_cooldown_until = max(self._global_cooldown_until, retry_after_epoch)
            if video_id:
                self._video_cooldown_until[video_id] = max(
                    self._video_cooldown_until.get(video_id, 0.0), retry_after_epoch,
                )

    def record_auth_required(self, video_id: str = "", reason: str = "") -> None:
        message = reason or DEFAULT_AUTH_INSTRUCTIONS
        with self._lock:
            self._auth_suspended_reason = message
            if video_id:
                self._auth_suspended_videos[video_id] = message

    def clear_auth_suspension(self, video_id: str = "") -> None:
        with self._lock:
            self._auth_suspended_reason = ""
            if video_id:
                self._auth_suspended_videos.pop(video_id, None)

    def cooldown_remaining(self, video_id: str = "", *, now: float | None = None) -> tuple[float, float | None]:
        current = time.time() if now is None else now
        with self._lock:
            until = self._global_cooldown_until
            if video_id:
                until = max(until, self._video_cooldown_until.get(video_id, 0.0))
            if until > current:
                return until - current, until
            if self._global_cooldown_until <= current:
                self._global_cooldown_until = 0.0
            if video_id and self._video_cooldown_until.get(video_id, 0.0) <= current:
                self._video_cooldown_until.pop(video_id, None)
            return 0.0, None

    def check_request_allowed(
        self,
        video_id: str = "",
        *,
        allow_explicit_auth_retry: bool = False,
        now: float | None = None,
    ) -> tuple[bool, str, float, float | None]:
        """Check whether a YouTube request is currently permitted.

        Returns `(allowed, block_category, remaining_seconds, retry_after_epoch)`.
        """
        remaining, until = self.cooldown_remaining(video_id, now=now)
        if remaining > 0.0:
            return False, "http_429", remaining, until
        with self._lock:
            auth_blocked = bool(self._auth_suspended_reason or (video_id and video_id in self._auth_suspended_videos))
            if auth_blocked:
                if allow_explicit_auth_retry:
                    self._auth_suspended_reason = ""
                    if video_id:
                        self._auth_suspended_videos.pop(video_id, None)
                else:
                    return False, "auth_required", 0.0, None
        return True, "", 0.0, None


_COORDINATOR = DownloadCoordinator()


def download_coordinator() -> DownloadCoordinator:
    return _COORDINATOR
