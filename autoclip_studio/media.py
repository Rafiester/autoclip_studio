from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .paths import CACHE_DIR
from .process import ProcessManager, find_media_tool


YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}


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
    elif parsed.path.startswith(("/shorts/", "/embed/")):
        candidate = parsed.path.split("/")[2]
    else:
        candidate = ""
    return candidate if re.fullmatch(r"[\w-]{6,20}", candidate) else None


def acquire_source(source: str, cache: bool, manager: ProcessManager,
                   log: callable = lambda _line: None) -> Path:
    local = Path(source).expanduser()
    if local.is_file():
        return local.resolve()
    if not is_youtube_url(source):
        raise ValueError("Choose an existing local video or enter a valid YouTube URL.")
    video_id = youtube_id(source) or "video"
    target_dir = CACHE_DIR / "downloads"
    target_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(target_dir.glob(f"{video_id}.*"))
    existing = [item for item in existing if item.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov"}]
    if cache and existing:
        log(f"Using cached download: {existing[0].name}")
        return existing[0]
    template = str(target_dir / f"{video_id}.%(ext)s")
    command = [
        sys.executable, "-m", "yt_dlp", "--no-playlist", "--newline", "--restrict-filenames",
        "--merge-output-format", "mp4", "-f",
        "bv*[height<=1080]+ba/b[height<=1080]/best", "-o", template, source,
    ]
    ffmpeg = Path(find_media_tool("ffmpeg"))
    command[3:3] = ["--ffmpeg-location", str(ffmpeg.parent)]
    manager.run(command, on_output=log)
    results = sorted(target_dir.glob(f"{video_id}.*"), key=lambda item: item.stat().st_mtime, reverse=True)
    if not results:
        raise RuntimeError("yt-dlp completed but produced no video file.")
    return results[0]


def probe(path: Path, manager: ProcessManager | None = None) -> dict:
    runner = manager or ProcessManager()
    result = runner.run([
        find_media_tool("ffprobe"), "-v", "error", "-show_entries",
        "format=duration:stream=index,codec_type,codec_name,width,height,avg_frame_rate",
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

