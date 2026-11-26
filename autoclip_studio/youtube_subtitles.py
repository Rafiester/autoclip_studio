"""YouTube manual subtitle and automatic caption extraction, normalization, and validation."""
from __future__ import annotations

import html
import json
import re
from pathlib import Path
from subprocess import CalledProcessError
from tempfile import TemporaryDirectory
from typing import Callable

from .download_state import DEFAULT_AUTH_INSTRUCTIONS, download_coordinator, format_cooldown
from .media import parse_retry_after, youtube_watch_url, yt_dlp_command
from .models import Segment, Transcript
from .paths import CACHE_DIR
from .process import ProcessManager
from .subtitles import detect_subtitle_language
from .youtube_metadata import YouTubeMetadata


_VTT_TIMECODE_RE = re.compile(
    r"^\s*(?:(\d{1,3}):)?(\d{2}):(\d{2})[.,](\d{3})\s*-->\s*(?:(\d{1,3}):)?(\d{2}):(\d{2})[.,](\d{3})"
)
_TAG_RE = re.compile(r"<[^>]+>")


def _parse_vtt_ts(h: str | None, m: str, s: str, ms: str) -> float:
    return int(h or 0) * 3600.0 + int(m) * 60.0 + int(s) + int(ms) / 1000.0


def _clean_cue_text(raw_lines: list[str]) -> str:
    cleaned_lines: list[str] = []
    for line in raw_lines:
        stripped = _TAG_RE.sub("", line)
        stripped = html.unescape(stripped)
        stripped = " ".join(stripped.split()).strip()
        if stripped and (not cleaned_lines or cleaned_lines[-1].casefold() != stripped.casefold()):
            cleaned_lines.append(stripped)
    return " ".join(cleaned_lines).strip()


def _strip_rolling_overlap(previous: str, current: str) -> str:
    """Remove duplicated prefix carried over by YouTube's rolling 2-line automatic captions."""
    prev_words = previous.split()
    curr_words = current.split()
    if not prev_words or not curr_words:
        return current
    prev_cf = [w.casefold() for w in prev_words]
    curr_cf = [w.casefold() for w in curr_words]
    if curr_cf == prev_cf:
        return ""
    # If current is a strict prefix/substring already spoken at the end of previous, skip it.
    if len(curr_cf) <= len(prev_cf) and prev_cf[-len(curr_cf):] == curr_cf:
        return ""
    max_overlap = min(len(prev_cf), len(curr_cf))
    for overlap in range(max_overlap, 0, -1):
        if prev_cf[-overlap:] == curr_cf[:overlap]:
            return " ".join(curr_words[overlap:]).strip()
    return current


def parse_vtt_text(vtt_text: str, *, is_auto: bool = False) -> list[Segment]:
    """Parse WebVTT subtitle text into clean, deduplicated timestamped Segments without fabricating word timings."""
    normalized = vtt_text.replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n\s*\n+", normalized)
    raw_cues: list[tuple[float, float, str]] = []

    for block in blocks:
        lines = [line.rstrip() for line in block.split("\n") if line.strip()]
        if not lines:
            continue
        header_upper = lines[0].strip().upper()
        if header_upper.startswith(("WEBVTT", "NOTE", "STYLE", "REGION", "KIND:", "LANGUAGE:")):
            continue
        tc_idx = -1
        match = None
        for idx, line in enumerate(lines[:2]):
            m = _VTT_TIMECODE_RE.match(line)
            if m:
                tc_idx = idx
                match = m
                break
        if match is None:
            continue
        start = _parse_vtt_ts(*match.groups()[:4])
        end = _parse_vtt_ts(*match.groups()[4:])
        if end <= start:
            continue
        text = _clean_cue_text(lines[tc_idx + 1:])
        if not text:
            continue
        raw_cues.append((start, end, text))

    segments: list[Segment] = []
    last_raw_text = ""
    for start, end, text in raw_cues:
        effective_text = _strip_rolling_overlap(last_raw_text, text) if is_auto else text
        last_raw_text = text
        if not effective_text:
            continue
        if segments and segments[-1].text.casefold() == effective_text.casefold() and start <= segments[-1].end + 0.5:
            segments[-1].end = max(segments[-1].end, end)
            continue
        # Ensure monotonic start without fabricating timestamps.
        if segments and start < segments[-1].end:
            if end <= segments[-1].end:
                # Tiny overlapping auto-caption line; append cleanly if distinct text.
                if is_auto and effective_text.casefold() not in segments[-1].text.casefold():
                    segments[-1].text = f"{segments[-1].text} {effective_text}".strip()
                continue
            start = segments[-1].end
        if end <= start:
            continue
        segments.append(Segment(start=round(start, 3), end=round(end, 3), text=effective_text, words=[]))
    return segments


def validate_youtube_transcript(
    segments: list[Segment],
    video_duration: float,
    *,
    min_coverage_ratio: float = 0.20,
) -> tuple[bool, str]:
    """Validate subtitle completeness and synchronization before skipping Speech-to-Text."""
    if not segments:
        return False, "No usable subtitle cues found."
    total_words = sum(len(seg.text.split()) for seg in segments)
    if total_words < 5:
        return False, "Subtitle track has too few words."
    covered_time = sum(max(0.0, seg.end - seg.start) for seg in segments)
    span_end = max(seg.end for seg in segments)
    if video_duration > 30.0:
        if span_end < video_duration * 0.35:
            return False, f"Subtitles end early ({span_end:.0f}s / {video_duration:.0f}s)."
        if covered_time / video_duration < min_coverage_ratio and len(segments) < 10:
            return False, f"Subtitle coverage too sparse ({covered_time / video_duration:.0%})."
    return True, f"Validated {len(segments)} cues ({total_words} words)."


def _match_lang(available: tuple[str, ...], preferred: str) -> str | None:
    if not available or not preferred:
        return None
    pref = preferred.strip().casefold()
    for code in available:
        if code.casefold() == pref:
            return code
    base = pref.split("-", 1)[0]
    for code in available:
        if code.casefold().split("-", 1)[0] == base:
            return code
    return None


def select_subtitle_track(
    metadata: YouTubeMetadata,
    preferred_language: str = "",
) -> tuple[str, bool] | None:
    """Select best YouTube subtitle track: manual in source/preferred language > manual > auto."""
    manual = metadata.subtitle_languages
    auto = metadata.auto_caption_languages
    lang_priorities = [item for item in (preferred_language, metadata.language, "id", "en") if item]

    # 1. Prefer manual subtitles in preferred or source language.
    for target in lang_priorities:
        if matched := _match_lang(manual, target):
            return matched, False
    # 2. Any manual subtitle if available (without silently translating).
    if manual:
        return manual[0], False
    # 3. Automatic captions in preferred or source language.
    for target in lang_priorities:
        if matched := _match_lang(auto, target):
            return matched, True
    if auto:
        return auto[0], True
    return None


def fetch_youtube_transcript(
    video_id: str,
    manager: ProcessManager,
    *,
    metadata: YouTubeMetadata | None = None,
    preferred_language: str = "",
    video_duration: float = 0.0,
    cache_enabled: bool = True,
    log: Callable[[str], None] | None = None,
) -> tuple[Transcript, str] | None:
    """Download and normalize YouTube subtitles/auto-captions into a cue-level Transcript."""
    if metadata is None:
        return None
    chosen = select_subtitle_track(metadata, preferred_language=preferred_language)
    if chosen is None:
        if log:
            log("No YouTube subtitles or automatic captions detected; falling back to Speech-to-Text.")
        return None

    lang_code, is_auto = chosen
    kind_label = "automatic captions" if is_auto else "manual subtitles"
    cache_path = CACHE_DIR / "youtube-subtitles" / f"{video_id}_{lang_code}_{'auto' if is_auto else 'manual'}.json"
    if cache_enabled and cache_path.is_file():
        try:
            cached = Transcript.from_dict(json.loads(cache_path.read_text(encoding="utf-8")))
            ok, _ = validate_youtube_transcript(cached.segments, video_duration or cached.duration)
            if ok:
                if log:
                    log(f"Using cached YouTube {kind_label} ({lang_code}, {len(cached.segments)} cues)")
                return cached, f"YouTube {kind_label} ({lang_code})"
        except (OSError, ValueError, KeyError, TypeError):
            pass

    coordinator = download_coordinator()
    allowed, block_cat, remaining, _until = coordinator.check_request_allowed(video_id)
    if not allowed:
        if log:
            if block_cat == "http_429":
                log(
                    f"Skipping YouTube subtitle request during rate-limit cooldown ({format_cooldown(remaining)}); "
                    "falling back to local Speech-to-Text."
                )
            else:
                log("Skipping YouTube subtitle request while verification is required; falling back to local Speech-to-Text.")
        return None

    def _download_vtt() -> str | None:
        with TemporaryDirectory(prefix="autoclip-ytsub-") as tmp_dir:
            out_tpl = str(Path(tmp_dir) / "%(id)s.%(ext)s")
            sub_flag = "--write-auto-subs" if is_auto else "--write-subs"
            command = yt_dlp_command() + [
                "--ignore-config", "--no-playlist", "--skip-download",
                sub_flag, "--sub-langs", lang_code, "--sub-format", "vtt",
                "--socket-timeout", "10", "--retries", "1", "--extractor-retries", "1",
                "--sleep-requests", "0.75",
                "-o", out_tpl, youtube_watch_url(video_id),
            ]
            try:
                manager.run(command, output_tail=20)
            except CalledProcessError as error:
                raw = error.output or ""
                low = raw.casefold()
                if any(tok in low for tok in ("http error 429", "too many requests", "rate limit")):
                    coordinator.record_rate_limit(video_id, retry_after_seconds=parse_retry_after(raw))
                elif any(tok in low for tok in ("sign in to confirm", "confirm you're not a bot", "confirm you’re not a bot")):
                    coordinator.record_auth_required(video_id, DEFAULT_AUTH_INSTRUCTIONS)
                if log:
                    log(f"Could not download YouTube {kind_label} ({lang_code}): {raw or error}")
                return None
            vtt_files = sorted(Path(tmp_dir).glob("*.vtt"))
            if not vtt_files:
                if log:
                    log(f"YouTube returned no VTT file for {kind_label} ({lang_code}); falling back to Speech-to-Text.")
                return None
            return vtt_files[0].read_text(encoding="utf-8", errors="replace")

    vtt_text = coordinator.deduplicate_extraction(f"sub:{video_id}:{lang_code}:{'auto' if is_auto else 'manual'}", _download_vtt)
    if not vtt_text:
        return None

    segments = parse_vtt_text(vtt_text, is_auto=is_auto)
    effective_duration = max(float(video_duration or 0.0), max((seg.end for seg in segments), default=0.0))
    ok, detail = validate_youtube_transcript(segments, effective_duration)
    if not ok:
        if log:
            log(f"YouTube {kind_label} ({lang_code}) skipped: {detail} Falling back to Speech-to-Text.")
        return None

    detected_lang = lang_code.split("-", 1)[0].casefold()
    if not detected_lang or detected_lang == "und":
        detected_lang, _ = detect_subtitle_language("\n".join(seg.text for seg in segments[:40]))
    transcript = Transcript(duration=effective_duration, language=detected_lang, segments=segments)
    if cache_enabled:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(transcript.to_dict(), ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
    if log:
        log(f"Extracted YouTube {kind_label} ({lang_code}): {detail}")
    return transcript, f"YouTube {kind_label} ({lang_code})"
