from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from subprocess import CalledProcessError
from tempfile import NamedTemporaryFile
from time import time
from urllib.parse import urlsplit

import requests

from .heatmap import parse_heatmap
from .media import youtube_id, youtube_watch_url, yt_dlp_command
from .paths import CACHE_DIR
from .process import ProcessManager


_CACHE_TTL = 24 * 60 * 60
_CACHE_SCHEMA_VERSION = 2
_MAX_THUMBNAIL_BYTES = 3 * 1024 * 1024
_METADATA_FIELDS = "%(.{id,title,channel,uploader,duration,view_count,upload_date,availability,formats,thumbnails,thumbnail,heatmap,subtitles,automatic_captions,language})j"


class YouTubeMetadataError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class YouTubeMetadata:
    video_id: str
    title: str
    channel: str
    duration: int | None
    resolution: str
    view_count: int | None
    upload_date: str | None
    heatmap: tuple[dict[str, float], ...] = ()
    subtitle_languages: tuple[str, ...] = ()
    auto_caption_languages: tuple[str, ...] = ()
    language: str = ""

    @property
    def has_heatmap(self) -> bool:
        return bool(self.heatmap)

    @property
    def has_subtitles(self) -> bool:
        return bool(self.subtitle_languages or self.auto_caption_languages)


def _nonnegative_int(value: object) -> int | None:
    if type(value) in (int, float) and 0 <= value < float("inf"):
        return int(value)
    return None


def _highest_resolution(info: dict) -> str:
    dimensions = []
    for stream in info.get("formats") or [info]:
        if stream.get("vcodec") == "none" or stream.get("protocol") == "mhtml" or stream.get("has_drm"):
            continue
        width, height = (_nonnegative_int(stream.get(key)) for key in ("width", "height"))
        if width and height:
            dimensions.append((width, height))
    if not dimensions:
        return ""
    width, height = max(dimensions, key=lambda size: (size[0] * size[1], size[1]))
    return f"{width} × {height}"


def _date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None


def _extraction_error(output: str) -> str:
    detail = output.casefold()
    if "private video" in detail or "this video is private" in detail:
        return "This YouTube video is private."
    if any(word in detail for word in ("sign in", "log in", "login required", "confirm you’re not a bot")):
        return "YouTube requires sign-in or verification for this video."
    if any(word in detail for word in ("video unavailable", "video is unavailable", "not available", "removed", "copyright",
                                        "has been terminated", "members-only")):
        return "This YouTube video is unavailable."
    if any(word in detail for word in ("timed out", "network", "connection", "unable to download",
                                        "getaddrinfo", "certificate", "http error", "winerror")):
        return "Could not reach YouTube. Check your connection and retry."
    return "YouTube metadata could not be retrieved. Retry or check the link in your browser."


def _thumbnail_urls(info: dict) -> list[str]:
    def rank(item: dict) -> float:
        width = item.get("width") or 0
        name = str(item.get("id", ""))
        return max(width, 1280 if "maxres" in name else 0)

    ranked = sorted(info.get("thumbnails") or [], key=rank, reverse=True)
    candidates = dict.fromkeys([item.get("url") for item in ranked] + [info.get("thumbnail")])
    urls = []
    for url in candidates:
        if not isinstance(url, str):
            continue
        parsed = urlsplit(url)
        if parsed.scheme == "https" and parsed.hostname in {"i.ytimg.com", "i9.ytimg.com", "img.youtube.com"}:
            urls.append(url)
    return urls


def _fetch_thumbnail(urls: list[str], manager: ProcessManager) -> bytes:
    for url in urls[:2]:
        manager.check_cancelled()
        try:
            with requests.get(url, stream=True, timeout=(3, 3), allow_redirects=False) as result:
                if result.status_code != 200 or not result.headers.get("Content-Type", "").startswith("image/"):
                    continue
                data = bytearray()
                for chunk in result.iter_content(32 * 1024):
                    manager.check_cancelled()
                    data.extend(chunk)
                    if len(data) > _MAX_THUMBNAIL_BYTES:
                        break
                else:
                    if data:
                        return bytes(data)
        except requests.RequestException:
            continue
    return b""


def _save_cache(path: Path, data: bytes) -> None:
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(dir=path.parent, delete=False) as pending:
            temporary = Path(pending.name)
            pending.write(data)
        temporary.replace(path)
    except OSError:
        pass
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _extract(video_id: str, manager: ProcessManager) -> tuple[YouTubeMetadata, list[str]]:
    command = yt_dlp_command() + [
        "--ignore-config", "--no-playlist", "--skip-download", "--simulate", "--print", _METADATA_FIELDS,
        "--ignore-no-formats-error", "--socket-timeout", "8", "--retries", "1", "--extractor-retries", "1",
        "--encoding", "utf-8", youtube_watch_url(video_id),
    ]
    try:
        output = manager.run(command, output_tail=24).stdout
    except CalledProcessError as error:
        raise YouTubeMetadataError(_extraction_error(error.output or "")) from error
    lines = output.splitlines()
    info = next((line for line in reversed(lines) if line.startswith("{")), "")
    diagnostics = "\n".join(line for line in lines if line != info)
    try:
        data = json.loads(info)
    except ValueError:
        raise YouTubeMetadataError(_extraction_error(diagnostics)) from None
    availability = data.get("availability")
    if availability == "private":
        raise YouTubeMetadataError("This YouTube video is private.")
    if availability == "needs_auth":
        raise YouTubeMetadataError("YouTube requires sign-in or verification for this video.")
    if availability in {"subscriber_only", "premium_only"}:
        raise YouTubeMetadataError("This YouTube video is unavailable without the required membership.")
    if data.get("id") != video_id or "entries" in data or not data.get("title"):
        raise YouTubeMetadataError("This YouTube video is unavailable.")
    title = " ".join(data["title"].split())
    if not title or title == f"youtube video #{video_id}":
        raise YouTubeMetadataError(_extraction_error(diagnostics))
    duration_val = _nonnegative_int(data.get("duration"))
    heatmap_points = parse_heatmap(data.get("heatmap"), duration=duration_val)
    heatmap_dicts = tuple(pt.to_dict() for pt in heatmap_points)

    def _lang_keys(mapping: object) -> tuple[str, ...]:
        if not isinstance(mapping, dict):
            return ()
        return tuple(
            str(k) for k, v in mapping.items()
            if isinstance(k, str) and k.strip() and k != "live_chat" and isinstance(v, list) and v
        )

    metadata = YouTubeMetadata(
        video_id, title, data.get("channel") or data.get("uploader") or "",
        duration_val, _highest_resolution(data),
        _nonnegative_int(data.get("view_count")), _date(data.get("upload_date")),
        heatmap=heatmap_dicts,
        subtitle_languages=_lang_keys(data.get("subtitles")),
        auto_caption_languages=_lang_keys(data.get("automatic_captions")),
        language=str(data.get("language") or "").strip(),
    )
    return metadata, _thumbnail_urls(data)


def fetch_youtube_metadata(video_id: str, manager: ProcessManager, *,
                           cache_dir: Path | None = None) -> tuple[YouTubeMetadata, bytes]:
    if youtube_id(youtube_watch_url(video_id)) != video_id:
        raise YouTubeMetadataError("Enter a complete link to a YouTube video or Short.")
    manager.check_cancelled()
    cache = cache_dir if cache_dir is not None else CACHE_DIR / "youtube-metadata"
    record_path, thumbnail_path = cache / f"{video_id}.json", cache / f"{video_id}.thumb"
    try:
        if time() - record_path.stat().st_mtime > _CACHE_TTL:
            raise ValueError("Expired metadata")
        record = json.loads(record_path.read_bytes())
        if int(record.get("schema_version") or 1) < _CACHE_SCHEMA_VERSION:
            raise ValueError("Upgrade cached metadata to include heatmap and subtitle tracks")
        raw_meta = dict(record["metadata"])
        raw_meta["heatmap"] = tuple(raw_meta.get("heatmap") or ())
        raw_meta["subtitle_languages"] = tuple(raw_meta.get("subtitle_languages") or ())
        raw_meta["auto_caption_languages"] = tuple(raw_meta.get("auto_caption_languages") or ())
        metadata = YouTubeMetadata(**raw_meta)
        if metadata.video_id != video_id:
            raise ValueError("Cached video ID mismatch")
        urls = record["thumbnails"]
    except (OSError, ValueError, TypeError, KeyError):
        metadata, urls = _extract(video_id, manager)
        manager.check_cancelled()
        _save_cache(
            record_path,
            json.dumps({
                "schema_version": _CACHE_SCHEMA_VERSION,
                "metadata": asdict(metadata),
                "thumbnails": urls,
            }).encode("utf-8"),
        )
    try:
        thumbnail = thumbnail_path.read_bytes()
    except OSError:
        thumbnail = b""
    if not thumbnail:
        thumbnail = _fetch_thumbnail(urls, manager)
        if thumbnail:
            _save_cache(thumbnail_path, thumbnail)
    manager.check_cancelled()
    return metadata, thumbnail
