from __future__ import annotations

import hashlib
import json
import re
import sys
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from tempfile import TemporaryDirectory, mkdtemp
from typing import Callable
from urllib.parse import parse_qs, urlparse

from .paths import CACHE_DIR, bundled_binary
from .process import ProcessManager, find_media_tool


YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}
MEDIA_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov"}
_TRANSFER_PREFIX = "AUTOCLIP_TRANSFER\t"
# Expose the CLI's downloader hook fields in both Python and bundled executable builds.
_TRANSFER_TEMPLATE = "download:" + _TRANSFER_PREFIX + "\t".join((
    "%(progress.status)s", "%(progress.downloaded_bytes)j", "%(progress.total_bytes)j",
    "%(progress.total_bytes_estimate)j", "%(progress.speed)j", "%(progress.eta)j",
    "%(info.vcodec)s", "%(info.acodec)s", "%(progress._default_template)s",
))


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


def yt_dlp_command() -> list[str]:
    downloader = bundled_binary("yt-dlp")
    python = Path(sys.executable)
    if python.name.lower() == "pythonw.exe":
        python = python.with_name("python.exe")
    if getattr(sys, "frozen", False) and downloader is None:
        raise FileNotFoundError("The bundled yt-dlp executable is missing. Restore the complete application folder.")
    command = [str(downloader)] if downloader else [str(python), "-m", "yt_dlp"]
    deno = bundled_binary("deno")
    if deno:
        command += ["--js-runtimes", f"deno:{deno}"]
    return command + ["--cache-dir", str(CACHE_DIR / "yt-dlp")]


def acquire_source(source: str, cache: bool, manager: ProcessManager,
                   log: Callable[[str], None] = lambda _line: None, *, job_dir: Path | None = None,
                   progress: Callable[[DownloadProgress], None] | None = None) -> Path:
    local = Path(source).expanduser()
    if local.is_file():
        return local.resolve()
    if not is_youtube_url(source):
        raise ValueError("Choose an existing local video or enter a valid YouTube URL.")
    if error := youtube_source_error(source):
        raise ValueError(error)
    manager.check_cancelled()
    key = youtube_id(source) or "url-" + hashlib.sha256(source.encode()).hexdigest()[:24]
    if not cache:
        target = job_dir / "download" if job_dir else Path(mkdtemp(prefix="autoclip-download-"))
        target.mkdir(parents=True, exist_ok=True)
        return _download_youtube(source, target, key, manager, log, progress=progress)

    downloads = CACHE_DIR / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    for item in sorted(downloads.glob(f"{key}.*")):
        if item.suffix.lower() in MEDIA_EXTENSIONS and item.is_file() and item.stat().st_size:
            log(f"Using cached download: {item.name}")
            return item
    with TemporaryDirectory(prefix="incoming-", dir=downloads) as staging:
        video = _download_youtube(source, Path(staging), key, manager, log, progress=progress)
        destination = downloads / video.name
        video.replace(destination)
        return destination


def _download_youtube(source: str, target: Path, key: str, manager: ProcessManager,
                      log: Callable[[str], None], *, progress: Callable[[DownloadProgress], None] | None = None) -> Path:
    template = str(target / f"{key}.%(ext)s")
    receipt = target / "downloaded-path.txt"
    command = yt_dlp_command()
    on_output = log
    if progress is not None:
        command += ["--progress", "--progress-delta", "0.2", "--progress-template", _TRANSFER_TEMPLATE]
        on_output = lambda line: _log_transfer(line, log, progress)
    command += [
        "--no-playlist", "--no-simulate", "--newline", "--restrict-filenames",
        "--ffmpeg-location", str(Path(find_media_tool("ffmpeg")).parent),
        "--merge-output-format", "mp4", "-f",
        "bv*[height<=1080]+ba/b[height<=1080]/best", "-o", template,
        "--print-to-file", "after_move:filepath", str(receipt), source,
    ]
    manager.run(command, on_output=on_output, output_tail=40)
    if receipt.is_file():
        paths = receipt.read_text(encoding="utf-8").splitlines()
        receipt.unlink()
        if paths:
            result = Path(paths[-1]).resolve()
            if result.parent == target.resolve() and result.is_file() and result.stat().st_size:
                return result
    raise RuntimeError("yt-dlp completed but produced no video file.")


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

