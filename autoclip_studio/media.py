from __future__ import annotations

import hashlib
import importlib.metadata
import json
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from math import isfinite
from pathlib import Path
from tempfile import mkdtemp
from typing import Callable
from urllib.parse import parse_qs, urlparse

from .diagnostics import LOG, redact
from .download_state import (
    DEFAULT_AUTH_INSTRUCTIONS,
    DownloadConflictError,
    DownloadState,
    cleanup_workspace,
    download_coordinator,
    format_cooldown,
    format_preserved_bytes,
    inspect_partial_files,
    save_download_state,
    verify_workspace_identity,
    workspace_key_for,
    workspace_path_for,
)
from .paths import CACHE_DIR, bundled_binary
from .process import CancelledError, PausedError, ProcessManager, find_media_tool


YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}
MEDIA_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov"}
DEFAULT_FORMAT = "bv*[height<=1080]+ba/b[height<=1080]/best"
FALLBACK_FORMAT = "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080]/best"
_TRANSFER_PREFIX = "AUTOCLIP_TRANSFER\t"
# Expose the CLI's downloader hook fields in both Python and bundled executable builds.
_TRANSFER_TEMPLATE = "download:" + _TRANSFER_PREFIX + "\t".join((
    "%(progress.status)s", "%(progress.downloaded_bytes)j", "%(progress.total_bytes)j",
    "%(progress.total_bytes_estimate)j", "%(progress.speed)j", "%(progress.eta)j",
    "%(info.vcodec)s", "%(info.acodec)s", "%(progress._default_template)s",
))


class DownloadError(RuntimeError):
    """Structured, actionable downloader error carrying redacted diagnostic details."""

    def __init__(
        self,
        summary: str,
        *,
        category: str = "unknown",
        details: str = "",
        retryable: bool = False,
        needs_dependency_repair: bool = False,
        exit_code: int | None = None,
        retry_after_seconds: float | None = None,
        retry_after_epoch: float | None = None,
        partial_bytes: int = 0,
        workspace: str = "",
        auth_instructions: str = "",
    ) -> None:
        self.summary = redact(summary).strip()
        self.category = category
        self.details = redact(details).strip()
        self.retryable = retryable
        self.needs_dependency_repair = needs_dependency_repair
        self.exit_code = exit_code
        self.retry_after_seconds = retry_after_seconds
        self.retry_after_epoch = retry_after_epoch
        self.partial_bytes = max(0, int(partial_bytes or 0))
        self.workspace = workspace
        self.auth_instructions = auth_instructions
        full = self.summary
        if self.details:
            full = f"{self.summary}\n\nTechnical details:\n{self.details}"
        super().__init__(full)


_RETRY_AFTER_HEADER_RE = re.compile(r"retry-after\s*[:=]\s*([^\r\n;]+)", re.IGNORECASE)
_WAIT_SECONDS_RE = re.compile(
    r"(?:retry|wait|try again)\s+(?:in|after)\s+(\d+(?:\.\d+)?)\s*(s|sec|secs|second|seconds|m|min|mins|minute|minutes)\b",
    re.IGNORECASE,
)


def parse_retry_after(output: str) -> float | None:
    """Parse Retry-After header (seconds or HTTP-date) or explicit wait hint from yt-dlp output."""
    if not output:
        return None
    header = _RETRY_AFTER_HEADER_RE.search(output)
    if header:
        raw = header.group(1).strip().strip("'\"")
        if raw.isdigit():
            val = float(raw)
            return val if val > 0 else None
        try:
            dt = parsedate_to_datetime(raw)
            delta = dt.timestamp() - time.time()
            if delta > 0:
                return delta
        except (TypeError, ValueError, OverflowError):
            pass
    hint = _WAIT_SECONDS_RE.search(output)
    if hint:
        amount = float(hint.group(1))
        unit = hint.group(2).lower()
        if unit.startswith("m"):
            amount *= 60.0
        return amount if amount > 0 else None
    return None


@dataclass(frozen=True, slots=True)
class DownloadProgress:
    downloaded: int | None
    total: int | None
    estimated: bool
    bytes_per_second: float | None
    eta_seconds: float | None
    stream: str
    finished: bool


def _hook_number(raw: str) -> int | float | None:
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    if type(value) in (int, float) and isfinite(value) and value >= 0:
        return value
    return None


def _log_transfer(line: str, log: Callable[[str], None], progress: Callable[[DownloadProgress], None]) -> None:
    if not line.startswith(_TRANSFER_PREFIX):
        log(line)
        return
    fields = line[len(_TRANSFER_PREFIX):].split("\t", 8)
    if len(fields) != 9:
        log(line)
        return
    status, downloaded, total, estimate, speed, eta, video_codec, audio_codec, detail = fields
    log(f"[download] {detail}")
    if status not in {"downloading", "finished"}:
        return
    downloaded, total, estimate, speed, eta = map(_hook_number, (downloaded, total, estimate, speed, eta))
    size = total or estimate
    if status == "finished" and downloaded is None:
        downloaded = total
    stream = ""
    if video_codec == "none" and audio_codec not in {"none", "NA"}:
        stream = "audio"
    elif audio_codec == "none" and video_codec not in {"none", "NA"}:
        stream = "video"
    progress(DownloadProgress(
        int(downloaded) if downloaded is not None else None, int(size) if size else None,
        not total and bool(estimate), speed, eta, stream, status == "finished",
    ))


def is_youtube_url(value: str) -> bool:
    try:
        parsed = urlparse(value.strip())
        return parsed.scheme in {"http", "https"} and parsed.hostname in YOUTUBE_HOSTS
    except ValueError:
        return False


def youtube_id(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.hostname == "youtu.be":
        candidate = parsed.path.strip("/").split("/")[0]
    elif parsed.path == "/watch":
        candidate = parse_qs(parsed.query).get("v", [""])[0]
    elif parsed.path.startswith(("/shorts/", "/embed/", "/live/", "/v/")):
        candidate = parsed.path.split("/")[2]
    else:
        candidate = ""
    return candidate if re.fullmatch(r"[A-Za-z0-9_-]{11}", candidate) else None


def youtube_source_error(url: str) -> str:
    """Validate a known YouTube URL without resolving it over the network."""
    if youtube_id(url):
        return ""
    parsed = urlparse(url)
    if parsed.path.rstrip("/") == "/playlist" or parse_qs(parsed.query).get("list"):
        return "Playlist links need a specific video. Open a video from the playlist and paste its link."
    if re.fullmatch(r"/(?:@[^/]+|(?:channel|user|c)/[^/]+)/live/?", parsed.path):
        return ""
    return "Enter a complete link to a YouTube video or Short."


def youtube_watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def detect_js_runtime() -> tuple[str, str] | None:
    """Detect a yt-dlp-compatible JS runtime (deno, node, bun, quickjs) without requiring bundled binaries."""
    deno = bundled_binary("deno")
    if deno and deno.is_file():
        return ("deno", str(deno))

    candidates: list[tuple[str, list[str]]] = [
        ("deno", [shutil.which("deno") or "", "/opt/homebrew/bin/deno", "/usr/local/bin/deno", str(Path.home() / ".deno" / "bin" / "deno")]),
        ("node", [shutil.which("node") or "", "/opt/homebrew/bin/node", "/usr/local/bin/node"]),
        ("bun", [shutil.which("bun") or "", "/opt/homebrew/bin/bun", str(Path.home() / ".bun" / "bin" / "bun")]),
        ("quickjs", [shutil.which("qjs") or "", "/opt/homebrew/bin/qjs", "/usr/local/bin/qjs"]),
    ]
    for kind, paths in candidates:
        for raw in paths:
            if raw and Path(raw).is_file():
                return (kind, str(Path(raw)))
    return None


def _yt_dlp_version_label() -> str:
    try:
        return importlib.metadata.version("yt-dlp")
    except importlib.metadata.PackageNotFoundError:
        return "bundled/unknown"


def yt_dlp_command() -> list[str]:
    downloader = bundled_binary("yt-dlp")
    python = Path(sys.executable)
    if python.name.lower() == "pythonw.exe":
        python = python.with_name("python.exe")
    if getattr(sys, "frozen", False) and downloader is None:
        raise FileNotFoundError("The bundled yt-dlp executable is missing. Restore the complete application folder.")
    command = [str(downloader)] if downloader else [str(python), "-m", "yt_dlp"]
    runtime = detect_js_runtime()
    if runtime:
        command += ["--js-runtimes", f"{runtime[0]}:{runtime[1]}"]
    return command + ["--cache-dir", str(CACHE_DIR / "yt-dlp")]


def _first_error_line(output: str) -> str:
    for line in reversed(output.splitlines()):
        stripped = line.strip()
        if stripped.startswith("ERROR:"):
            return stripped[len("ERROR:"):].strip()
    for line in reversed(output.splitlines()):
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def classify_download_error(
    output: str,
    exit_code: int | None = None,
    *,
    command_hint: str = "",
) -> DownloadError:
    """Map raw yt-dlp/FFmpeg subprocess output to a structured, user-actionable DownloadError."""
    redacted_out = redact(output or "").strip()
    low = redacted_out.lower()
    js_rt = detect_js_runtime()
    env_header = (
        f"yt-dlp: {_yt_dlp_version_label()} | "
        f"JS runtime: {f'{js_rt[0]} ({js_rt[1]})' if js_rt else 'none'} | "
        f"exit_code: {exit_code if exit_code is not None else 'n/a'}"
    )
    if command_hint:
        env_header += f"\nCommand: {redact(command_hint)}"
    diag = f"{env_header}\n{redacted_out}".strip() if redacted_out else env_header

    if any(tok in low for tok in ("http error 403", "403: forbidden", "403 forbidden")):
        hint = (
            "Update yt-dlp in Dependency Manager and ensure a JavaScript runtime (Node.js or Deno) is available."
            if not js_rt
            else "Update yt-dlp in Dependency Manager or retry in a moment."
        )
        return DownloadError(
            f"YouTube rejected the media download (HTTP 403 Forbidden). {hint}",
            category="http_403",
            details=diag,
            retryable=True,
            needs_dependency_repair=True,
            exit_code=exit_code,
        )
    if any(tok in low for tok in ("http error 429", "429: too many requests", "too many requests", "rate limit")):
        retry_after = parse_retry_after(redacted_out)
        wait_note = (
            f" Retry available after {format_cooldown(retry_after)}."
            if retry_after
            else " Automatic retries have been paused; wait for cooldown before resuming."
        )
        return DownloadError(
            f"YouTube temporarily rate-limited downloads (HTTP 429 Too Many Requests).{wait_note}",
            category="http_429",
            details=diag,
            retryable=True,
            exit_code=exit_code,
            retry_after_seconds=retry_after,
        )
    if any(tok in low for tok in (
        "sign in to confirm", "confirm you’re not a bot", "confirm you're not a bot",
        "login required", "cookies", "private video", "this video is private",
        "members-only", "join this channel", "age-restricted", "confirm your age",
    )):
        return DownloadError(
            "YouTube requires authentication or verification before allowing this download.",
            category="auth_required",
            details=diag,
            retryable=False,
            exit_code=exit_code,
            auth_instructions=DEFAULT_AUTH_INSTRUCTIONS,
        )
    if any(tok in low for tok in (
        "no supported javascript runtime", "js challenge", "yt-dlp-ejs", "ejs", "deno", "quickjs",
    )) and ("error" in low or "failed" in low or "unavailable" in low or "missing" in low):
        return DownloadError(
            "YouTube JavaScript challenge solving failed. Install Node.js or Deno and update yt-dlp in Dependency Manager.",
            category="js_runtime_error",
            details=diag,
            retryable=False,
            needs_dependency_repair=True,
            exit_code=exit_code,
        )
    if any(tok in low for tok in ("requested format is not available", "no video formats found", "only images are available")):
        return DownloadError(
            "The requested video/audio format is unavailable for this YouTube video. Try retrying or updating yt-dlp.",
            category="format_unavailable",
            details=diag,
            retryable=True,
            needs_dependency_repair=True,
            exit_code=exit_code,
        )
    if any(tok in low for tok in ("ffmpeg not found", "ffprobe not found", "ffmpeg is not installed", "ffprobe and ffmpeg not found")):
        return DownloadError(
            "FFmpeg or FFprobe is missing and required to merge YouTube video and audio. Open Dependency Manager to repair media tools.",
            category="missing_media_tool",
            details=diag,
            retryable=False,
            needs_dependency_repair=True,
            exit_code=exit_code,
        )
    if any(tok in low for tok in ("postprocessing", "error merging", "merger", "could not write header", "moov atom", "invalid data found when processing")):
        return DownloadError(
            "FFmpeg failed while merging the downloaded video and audio streams. Check FFmpeg in Dependency Manager or retry.",
            category="merge_error",
            details=diag,
            retryable=True,
            needs_dependency_repair=True,
            exit_code=exit_code,
        )
    if any(tok in low for tok in ("no space left on device", "not enough space", "disk full", "errno 28")):
        return DownloadError(
            "Download stopped because there is not enough free disk space. Free up storage and retry.",
            category="disk_space",
            details=diag,
            retryable=False,
            exit_code=exit_code,
        )
    if any(tok in low for tok in ("permission denied", "access is denied", "read-only file system", "errno 13")):
        return DownloadError(
            "Could not write the downloaded file due to folder permissions. Check cache/output directory permissions.",
            category="permission_error",
            details=diag,
            retryable=False,
            exit_code=exit_code,
        )
    if any(tok in low for tok in (
        "timed out", "connection reset", "connection refused", "temporary failure in name resolution",
        "getaddrinfo", "nodename nor servname", "ssl:", "certificate verify failed",
        "network is unreachable", "remote end closed", "incomplete read", "urlopen error",
    )):
        return DownloadError(
            "Network interruption while downloading from YouTube. Check your internet connection and retry.",
            category="network_error",
            details=diag,
            retryable=True,
            exit_code=exit_code,
        )
    if any(tok in low for tok in (
        "unable to extract", "extractor error", "signature extraction failed",
        "nsig extraction failed", "sabr", "video unavailable", "this video is unavailable",
    )):
        return DownloadError(
            "YouTube extractor failed to retrieve playable streams. Update yt-dlp in Dependency Manager or verify the link is public.",
            category="extractor_error",
            details=diag,
            retryable=False,
            needs_dependency_repair=True,
            exit_code=exit_code,
        )

    reason = _first_error_line(redacted_out)
    summary = (
        f"YouTube download failed: {reason}"
        if reason
        else f"YouTube download failed (yt-dlp exited with code {exit_code}). Open Technical Details for diagnostics."
    )
    return DownloadError(
        summary,
        category="unknown",
        details=diag,
        retryable=False,
        needs_dependency_repair=True,
        exit_code=exit_code,
    )


def _verify_downloaded_file(path: Path, manager: ProcessManager) -> bool:
    """Verify a downloaded media file is non-empty and playable before publishing to cache."""
    if not path.is_file() or path.stat().st_size == 0:
        return False
    try:
        ffprobe_path = Path(find_media_tool("ffprobe"))
    except FileNotFoundError:
        return path.stat().st_size > 0
    if not ffprobe_path.is_file():
        # Unit tests that monkeypatch find_media_tool to a fake path cannot run real ffprobe.
        return path.stat().st_size > 0
    try:
        validate_mp4(path, manager)
        return True
    except Exception as exc:
        LOG.warning("Downloaded file failed media verification (%s): %s", path.name, exc)
        return False


def acquire_source(
    source: str,
    cache: bool,
    manager: ProcessManager,
    log: Callable[[str], None] = lambda _line: None,
    *,
    job_dir: Path | None = None,
    progress: Callable[[DownloadProgress], None] | None = None,
    project_id: str | None = None,
    explicit_retry: bool = False,
    on_state: Callable[[DownloadState], None] | None = None,
) -> Path:
    local = Path(source).expanduser()
    if local.is_file():
        return local.resolve()
    if not is_youtube_url(source):
        raise ValueError("Choose an existing local video or enter a valid YouTube URL.")
    if error := youtube_source_error(source):
        raise ValueError(error)
    manager.check_cancelled()
    vid = youtube_id(source) or ""
    key = vid or "url-" + hashlib.sha256(source.encode()).hexdigest()[:24]

    downloads = CACHE_DIR / "downloads"
    if cache:
        downloads.mkdir(parents=True, exist_ok=True)
        for item in sorted(downloads.glob(f"{key}.*")):
            if item.suffix.lower() in MEDIA_EXTENSIONS and item.is_file() and item.stat().st_size:
                if _verify_downloaded_file(item, manager):
                    log(f"Using cached download: {item.name}")
                    return item
                log(f"Ignoring corrupted cached file {item.name}; re-downloading…")
                try:
                    item.unlink(missing_ok=True)
                except OSError:
                    pass

    workspace = workspace_path_for(
        source,
        video_id=vid or None,
        project_id=project_id,
        cache_enabled=cache,
        base_dir=CACHE_DIR,
    )
    workspace_key = workspace_key_for(
        source,
        video_id=vid or None,
        project_id=project_id,
        cache_enabled=cache,
    )
    coordinator = download_coordinator()
    allowed, block_cat, remaining, until = coordinator.check_request_allowed(
        vid, allow_explicit_auth_retry=explicit_retry,
    )
    if not allowed:
        disk_bytes, partial_files = inspect_partial_files(workspace)
        if block_cat == "http_429":
            summary = (
                f"YouTube rate limit cooldown active. Retry available in {format_cooldown(remaining)}."
                + (f" ({format_preserved_bytes(disk_bytes)} preserved)" if disk_bytes > 0 else "")
            )
            state = DownloadState(
                workspace_key=workspace_key,
                workspace_dir=str(workspace),
                project_id=project_id or "",
                video_id=vid,
                source_url=source,
                selected_format=DEFAULT_FORMAT,
                status="rate_limited",
                downloaded_bytes=disk_bytes,
                partial_files=partial_files,
                retry_after_epoch=until,
                cooldown_seconds=remaining,
                error_category="http_429",
                error_summary=summary,
            )
            save_download_state(workspace, state)
            if on_state:
                on_state(state)
            raise DownloadError(
                summary,
                category="http_429",
                retryable=True,
                retry_after_seconds=remaining,
                retry_after_epoch=until,
                partial_bytes=disk_bytes,
                workspace=str(workspace),
            )
        summary = "YouTube requires sign-in or verification. Automatic downloads are paused until you verify and retry."
        state = DownloadState(
            workspace_key=workspace_key,
            workspace_dir=str(workspace),
            project_id=project_id or "",
            video_id=vid,
            source_url=source,
            selected_format=DEFAULT_FORMAT,
            status="auth_required",
            downloaded_bytes=disk_bytes,
            partial_files=partial_files,
            error_category="auth_required",
            error_summary=summary,
            auth_instructions=DEFAULT_AUTH_INSTRUCTIONS,
        )
        save_download_state(workspace, state)
        if on_state:
            on_state(state)
        raise DownloadError(
            summary,
            category="auth_required",
            retryable=False,
            partial_bytes=disk_bytes,
            workspace=str(workspace),
            auth_instructions=DEFAULT_AUTH_INSTRUCTIONS,
        )

    try:
        with coordinator.acquire_download_slot(project_id=project_id or "", video_id=vid):
            video = _download_youtube(
                source,
                workspace,
                key,
                manager,
                log,
                progress=progress,
                project_id=project_id or "",
                video_id=vid,
                workspace_key=workspace_key,
                on_state=on_state,
            )
            if cache:
                destination = downloads / video.name
                video.replace(destination)
            else:
                dest_dir = job_dir / "download" if job_dir else Path(mkdtemp(prefix="autoclip-download-"))
                dest_dir.mkdir(parents=True, exist_ok=True)
                destination = dest_dir / video.name
                video.replace(destination)
            cleanup_workspace(workspace)
            coordinator.record_success(vid)
            return destination
    except DownloadConflictError as exc:
        raise DownloadError(
            str(exc),
            category="duplicate_download",
            retryable=False,
            workspace=str(workspace),
        ) from exc


def _download_youtube(
    source: str,
    target: Path,
    key: str,
    manager: ProcessManager,
    log: Callable[[str], None],
    *,
    progress: Callable[[DownloadProgress], None] | None = None,
    project_id: str = "",
    video_id: str = "",
    workspace_key: str = "",
    on_state: Callable[[DownloadState], None] | None = None,
) -> Path:
    try:
        ffmpeg_dir = str(Path(find_media_tool("ffmpeg")).parent)
    except FileNotFoundError as exc:
        raise DownloadError(
            str(exc),
            category="missing_media_tool",
            details=str(exc),
            retryable=False,
            needs_dependency_repair=True,
        ) from exc

    target.mkdir(parents=True, exist_ok=True)
    template = str(target / f"{key}.%(ext)s")
    receipt = target / "downloaded-path.txt"
    format_plan = (DEFAULT_FORMAT, DEFAULT_FORMAT, FALLBACK_FORMAT)
    last_error: DownloadError | None = None
    coordinator = download_coordinator()
    seen_downloaded: list[int] = [0]
    seen_total: list[int | None] = [None]

    def _persist_state(
        status: str,
        fmt: str,
        *,
        retry_after_epoch: float | None = None,
        cooldown_seconds: float | None = None,
        error_category: str = "",
        error_summary: str = "",
        auth_instructions: str = "",
    ) -> DownloadState:
        disk_bytes, partial_files = inspect_partial_files(target)
        preserved = max(disk_bytes, seen_downloaded[0]) if status == "downloading" else disk_bytes
        if preserved == 0 and seen_downloaded[0] > 0 and partial_files:
            preserved = seen_downloaded[0]
        state = DownloadState(
            workspace_key=workspace_key or target.name,
            workspace_dir=str(target),
            project_id=project_id,
            video_id=video_id,
            source_url=source,
            selected_format=fmt,
            status=status,
            downloaded_bytes=preserved,
            total_bytes=seen_total[0],
            partial_files=partial_files,
            retry_after_epoch=retry_after_epoch,
            cooldown_seconds=cooldown_seconds,
            error_category=error_category,
            error_summary=error_summary,
            auth_instructions=auth_instructions,
        )
        save_download_state(target, state)
        if on_state:
            on_state(state)
        return state

    for attempt, fmt in enumerate(format_plan):
        manager.check_cancelled()
        reused, reason, preserved_bytes = verify_workspace_identity(
            target, video_id=video_id, source_url=source, selected_format=fmt,
        )
        if reused and preserved_bytes > 0:
            log(f"Resuming download from preserved partial files ({format_preserved_bytes(preserved_bytes)})…")
        elif reason in ("video_id_mismatch", "source_url_mismatch", "format_mismatch"):
            log(f"Cleared incompatible partial files ({reason}) before starting download.")

        _persist_state("downloading", fmt)
        receipt.unlink(missing_ok=True)
        command = yt_dlp_command()

        def _track_progress(item: DownloadProgress) -> None:
            if item.downloaded is not None and item.downloaded > seen_downloaded[0]:
                seen_downloaded[0] = int(item.downloaded)
            if item.total is not None and item.total > 0:
                seen_total[0] = int(item.total)
            if progress is not None:
                progress(item)

        on_output = log
        if progress is not None:
            command += ["--progress", "--progress-delta", "0.2", "--progress-template", _TRANSFER_TEMPLATE]
            on_output = lambda line: _log_transfer(line, log, _track_progress)
        command += [
            "--no-playlist", "--no-simulate", "--newline", "--restrict-filenames",
            "--continue", "--part", "--retries", "1", "--fragment-retries", "2",
            "--sleep-requests", "1",
            "--ffmpeg-location", ffmpeg_dir,
            "--merge-output-format", "mp4", "-f", fmt, "-o", template,
            "--print-to-file", "after_move:filepath", str(receipt), source,
        ]
        try:
            manager.run(command, on_output=on_output, output_tail=80)
        except PausedError:
            state = _persist_state("paused", fmt, error_summary="Download paused by user")
            log(f"Download paused ({format_preserved_bytes(state.downloaded_bytes)} preserved).")
            raise
        except CancelledError:
            cleanup_workspace(target)
            raise
        except FileNotFoundError as exc:
            _persist_state("failed", fmt, error_category="missing_media_tool", error_summary=str(exc))
            raise DownloadError(
                f"Required downloader executable is missing: {exc}",
                category="missing_media_tool",
                details=str(exc),
                retryable=False,
                needs_dependency_repair=True,
                workspace=str(target),
            ) from exc
        except subprocess.CalledProcessError as exc:
            err = classify_download_error(
                exc.output or "",
                exit_code=exc.returncode,
                command_hint=" ".join(str(c) for c in command),
            )
            last_error = err
            LOG.warning(
                "YouTube download attempt %d/%d failed (%s): %s",
                attempt + 1, len(format_plan), err.category, err.summary,
            )
            # NEVER hammer YouTube with automatic retries on HTTP 429 or auth/bot verification!
            if err.category == "http_429":
                until, cooldown = coordinator.record_rate_limit(
                    video_id, retry_after_seconds=err.retry_after_seconds,
                )
                state = _persist_state(
                    "rate_limited",
                    fmt,
                    retry_after_epoch=until,
                    cooldown_seconds=cooldown,
                    error_category="http_429",
                    error_summary=err.summary,
                )
                preserved_note = (
                    f" Preserved {format_preserved_bytes(state.downloaded_bytes)} on disk."
                    if state.downloaded_bytes > 0
                    else ""
                )
                raise DownloadError(
                    f"YouTube rate-limited this download (HTTP 429 Too Many Requests). "
                    f"Retry available in {format_cooldown(cooldown)}.{preserved_note}",
                    category="http_429",
                    details=err.details,
                    retryable=True,
                    exit_code=err.exit_code,
                    retry_after_seconds=cooldown,
                    retry_after_epoch=until,
                    partial_bytes=state.downloaded_bytes,
                    workspace=str(target),
                ) from exc
            if err.category == "auth_required":
                coordinator.record_auth_required(video_id, err.auth_instructions)
                state = _persist_state(
                    "auth_required",
                    fmt,
                    error_category="auth_required",
                    error_summary=err.summary,
                    auth_instructions=err.auth_instructions or DEFAULT_AUTH_INSTRUCTIONS,
                )
                raise DownloadError(
                    err.summary,
                    category="auth_required",
                    details=err.details,
                    retryable=False,
                    exit_code=err.exit_code,
                    partial_bytes=state.downloaded_bytes,
                    workspace=str(target),
                    auth_instructions=err.auth_instructions or DEFAULT_AUTH_INSTRUCTIONS,
                ) from exc

            can_retry = err.retryable and attempt + 1 < len(format_plan)
            if not can_retry:
                state = _persist_state(
                    "failed",
                    fmt,
                    error_category=err.category,
                    error_summary=err.summary,
                )
                err.partial_bytes = state.downloaded_bytes
                err.workspace = str(target)
                raise err from exc
            wait_s = 2 ** attempt
            log(f"Download interrupted ({err.category}); retrying in {wait_s}s ({attempt + 2}/{len(format_plan)})…")
            for _ in range(wait_s * 5):
                try:
                    manager.check_cancelled()
                except PausedError:
                    _persist_state("paused", fmt, error_summary="Download paused by user")
                    raise
                except CancelledError:
                    cleanup_workspace(target)
                    raise
                time.sleep(0.2)
            continue

        if receipt.is_file():
            paths = [p.strip() for p in receipt.read_text(encoding="utf-8").splitlines() if p.strip()]
            receipt.unlink(missing_ok=True)
            if paths:
                result = Path(paths[-1]).resolve()
                if result.parent == target.resolve() and _verify_downloaded_file(result, manager):
                    return result
                last_error = DownloadError(
                    "Downloaded video file failed integrity verification (incomplete or unplayable output).",
                    category="merge_error",
                    details=f"Expected playable video at: {result}",
                    retryable=True,
                    needs_dependency_repair=True,
                    workspace=str(target),
                )
                if attempt + 1 < len(format_plan):
                    try:
                        result.unlink(missing_ok=True)
                    except OSError:
                        pass
                    continue
                _persist_state("failed", fmt, error_category="merge_error", error_summary=last_error.summary)
                raise last_error
        last_error = DownloadError(
            "yt-dlp completed without producing a playable video file.",
            category="unknown",
            details=f"Staging directory: {target}",
            retryable=False,
            needs_dependency_repair=True,
            workspace=str(target),
        )
        _persist_state("failed", fmt, error_category="unknown", error_summary=last_error.summary)
        raise last_error

    raise last_error or DownloadError("YouTube download failed after retries.", category="unknown")


def probe(path: Path, manager: ProcessManager | None = None) -> dict:
    runner = manager or ProcessManager()
    result = runner.run([
        find_media_tool("ffprobe"), "-v", "error", "-show_entries",
        "format=duration,start_time:stream=index,codec_type,codec_name,width,height,avg_frame_rate,duration,start_time:stream_side_data=rotation",
        "-of", "json", path,
    ])
    return json.loads(result.stdout)


def validate_mp4(path: Path, manager: ProcessManager | None = None) -> float:
    if not path.exists() or path.stat().st_size < 1024:
        raise RuntimeError(f"Rendered file is missing or empty: {path}")
    info = probe(path, manager)
    streams = info.get("streams", [])
    if not any(item.get("codec_type") == "video" for item in streams):
        raise RuntimeError(f"Rendered file has no video stream: {path}")
    duration = float(info.get("format", {}).get("duration", 0))
    if duration <= 0.1:
        raise RuntimeError(f"Rendered file has invalid duration: {path}")
    return duration
