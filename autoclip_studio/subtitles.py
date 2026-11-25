"""External SubRip (.srt) parser, validator, and lightweight language detector."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .models import Segment, Transcript


_TIMECODE_RE = re.compile(
    r"^\s*(\d{1,3}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d{1,3}):(\d{2}):(\d{2})[,.](\d{1,3})"
)

_ID_WORDS = frozenset({
    "yang", "dan", "di", "ke", "dari", "ini", "itu", "tidak", "tak", "untuk", "dengan",
    "adalah", "akan", "juga", "atau", "bisa", "dapat", "sudah", "telah", "saya", "aku",
    "kamu", "anda", "kita", "kami", "mereka", "karena", "jadi", "kalau", "jika", "ada",
    "pada", "dalam", "saat", "ketika", "lebih", "sangat", "banget", "apa", "kenapa",
})

_EN_WORDS = frozenset({
    "the", "and", "is", "are", "was", "were", "to", "of", "in", "for", "that", "this",
    "with", "you", "it", "not", "have", "has", "from", "they", "what", "when", "where",
    "why", "how", "can", "will", "just", "about", "because", "your", "we", "our",
})


class SrtValidationError(ValueError):
    """Raised when an imported .srt subtitle file is invalid or unreadable."""


@dataclass(frozen=True, slots=True)
class SrtValidationResult:
    transcript: Transcript
    cue_count: int
    detected_language: str
    language_label: str
    encoding: str


def _parse_timecode(hours: str, minutes: str, seconds: str, millis: str) -> float:
    h, m, s = int(hours), int(minutes), int(seconds)
    ms = int(millis.ljust(3, "0")[:3])
    if m >= 60 or s >= 60:
        raise SrtValidationError(f"Invalid SRT timecode values: {hours}:{minutes}:{seconds},{millis}")
    return h * 3600.0 + m * 60.0 + s + ms / 1000.0


def detect_subtitle_language(text: str) -> tuple[str, str]:
    tokens = re.findall(r"[a-zA-ZÀ-ÿ]+", text.casefold())
    if not tokens:
        return "unknown", "Unknown"
    id_hits = sum(1 for tok in tokens if tok in _ID_WORDS)
    en_hits = sum(1 for tok in tokens if tok in _EN_WORDS)
    if id_hits >= 2 and id_hits > en_hits:
        return "id", "Indonesian (Bahasa Indonesia)"
    if en_hits >= 2 and en_hits >= id_hits:
        return "en", "English"
    if id_hits > 0 and en_hits == 0:
        return "id", "Indonesian (Bahasa Indonesia)"
    if en_hits > 0 and id_hits == 0:
        return "en", "English"
    return "auto", "Auto / Other"


def _read_srt_text(path: Path) -> tuple[str, str]:
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig"), "UTF-8 BOM"
    try:
        return raw.decode("utf-8"), "UTF-8"
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace"), "CP1252"


def parse_srt_file(
    path: Path,
    *,
    offset_seconds: float = 0.0,
    fallback_duration: float = 0.0,
) -> SrtValidationResult:
    """Parse and validate an external .srt file into a cue-level Transcript without fabricating word timings."""
    if not path.is_file():
        raise SrtValidationError("Subtitle file does not exist.")
    if path.suffix.lower() != ".srt":
        raise SrtValidationError("Choose a valid .srt subtitle file.")

    content, encoding = _read_srt_text(path)
    normalized = content.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        raise SrtValidationError("The selected .srt file is empty.")

    blocks = re.split(r"\n\s*\n+", normalized)
    segments: list[Segment] = []
    all_text: list[str] = []

    for block in blocks:
        lines = [line.rstrip() for line in block.split("\n")]
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()
        if not lines:
            continue

        tc_idx = 0
        match = _TIMECODE_RE.match(lines[0])
        if not match and len(lines) >= 2 and lines[0].strip().isdigit():
            tc_idx = 1
            match = _TIMECODE_RE.match(lines[1])
        if not match:
            raise SrtValidationError(f"Malformed SRT cue block near: {lines[0][:60]}")

        start_raw = _parse_timecode(*match.groups()[:4])
        end_raw = _parse_timecode(*match.groups()[4:])
        if end_raw <= start_raw:
            raise SrtValidationError(f"Invalid cue range (end <= start) in block: {lines[tc_idx][:40]}")

        cue_text = "\n".join(lines[tc_idx + 1:]).strip()
        if not cue_text:
            continue

        start = max(0.0, start_raw + offset_seconds)
        end = max(start, end_raw + offset_seconds)
        if end <= start:
            continue

        segments.append(Segment(start=start, end=end, text=cue_text, words=[]))
        all_text.append(cue_text)

    if not segments:
        raise SrtValidationError("No valid subtitle cues were found in the selected .srt file.")

    lang_code, lang_label = detect_subtitle_language("\n".join(all_text))
    duration = max(float(fallback_duration or 0.0), max(seg.end for seg in segments))
    transcript = Transcript(duration=duration, language=lang_code, segments=segments)
    return SrtValidationResult(
        transcript=transcript,
        cue_count=len(segments),
        detected_language=lang_code,
        language_label=lang_label,
        encoding=encoding,
    )
