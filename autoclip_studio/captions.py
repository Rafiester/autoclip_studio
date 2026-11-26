from __future__ import annotations

import math
import os
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .models import Highlight, Transcript, Word

SYNC_MODES = ("precise", "readable")
DEFAULT_CAPTION_Y_RATIO = 0.73
CAPTION_Y_MIN = 0.12
CAPTION_Y_MAX = 0.88
_SENTENCE_END = frozenset(".!?:")
_CLAUSE_END = frozenset(",;")
_EDGE_BLEED_MIN_S = 0.05
_MAX_WORD_GAP_GROUP_S = 0.36
_HYPHEN_CHARS = frozenset("-\u2010\u2011\u2013\u2014")
_PUNCT_ATTACH = frozenset(".,!?:;…'\"”’)]")


@dataclass(frozen=True, slots=True)
class PlatformSafeArea:
    id: str
    label: str
    top: float
    bottom: float
    left: float
    right: float
    right_rail_top: float = 0.42


PLATFORM_SAFE_AREAS: dict[str, PlatformSafeArea] = {
    "all": PlatformSafeArea("all", "All Platforms", top=0.13, bottom=0.22, left=0.05, right=0.15, right_rail_top=0.40),
    "tiktok": PlatformSafeArea("tiktok", "TikTok", top=0.13, bottom=0.22, left=0.04, right=0.15, right_rail_top=0.42),
    "reels": PlatformSafeArea("reels", "Instagram Reels", top=0.12, bottom=0.21, left=0.04, right=0.15, right_rail_top=0.46),
    "shorts": PlatformSafeArea("shorts", "YouTube Shorts", top=0.12, bottom=0.20, left=0.04, right=0.15, right_rail_top=0.44),
}

_EFFECT_SCALE_PEAK: dict[str, float] = {
    "bounce": 1.22,
    "viral": 1.20,
    "punch": 1.11,
    "emphasis": 1.12,
    "box": 1.06,
    "glow": 1.14,
}


@dataclass(frozen=True, slots=True)
class CaptionSyncProfile:
    lead_in_s: float
    trailing_hold_s: float
    min_duration_s: float
    max_hold_extension_s: float
    bridge_word_gap_s: float
    bridge_cue_gap_s: float


_SYNC_PROFILES: dict[str, CaptionSyncProfile] = {
    "precise": CaptionSyncProfile(
        lead_in_s=0.0,
        trailing_hold_s=0.06,
        min_duration_s=0.38,
        max_hold_extension_s=0.30,
        bridge_word_gap_s=0.14,
        bridge_cue_gap_s=0.04,
    ),
    "readable": CaptionSyncProfile(
        lead_in_s=0.035,
        trailing_hold_s=0.11,
        min_duration_s=0.55,
        max_hold_extension_s=0.45,
        bridge_word_gap_s=0.18,
        bridge_cue_gap_s=0.05,
    ),
}


@dataclass(frozen=True, slots=True)
class CaptionCue:
    start: float
    end: float
    words: tuple[Word, ...]
    text: str
    has_word_timing: bool


@dataclass(frozen=True, slots=True)
class FaceZone:
    """Normalized output-frame face boxes [0..1] active during [start, end]."""
    start: float
    end: float
    boxes: tuple[tuple[float, float, float, float], ...]


def _font_advance_factor(font_name: str) -> float:
    low = font_name.casefold()
    if "impact" in low:
        return 0.50
    if "consolas" in low or "menlo" in low:
        return 0.60
    if "black" in low:
        return 0.63
    return 0.57


def estimate_caption_bbox(
    text: str,
    template: CaptionTemplate,
    width: int,
    height: int,
) -> tuple[float, float]:
    """Estimate normalized caption (width, height) including wrap lines and peak animation scale."""
    w = max(1, int(width))
    h = max(1, int(height))
    scale = min(w / 1080.0, h / 1080.0)
    margin_x = max(12, round(w * 0.065))
    avail_w = max(80.0, float(w - 2 * margin_x))

    display = text.upper() if template.uppercase else text
    words = display.split()
    longest = max((len(item) for item in words), default=1)
    fit_ratio = max(0.42, min(1.0, 18.0 / longest))
    font_px = float(max(12, round(template.size * scale * (fit_ratio if fit_ratio < 1.0 else 1.0))))
    char_w = font_px * _font_advance_factor(template.font)

    if template.effect == "bounce" and words:
        lines = [max(words, key=len)]
    else:
        lines = []
        for raw_line in (display or " ").splitlines():
            current = ""
            for token in raw_line.split():
                candidate = token if not current else f"{current} {token}"
                if current and len(candidate) * char_w > avail_w:
                    lines.append(current)
                    current = token
                else:
                    current = candidate
            if current or not lines:
                lines.append(current)
    line_count = max(1, len(lines))
    max_line_px = max((len(line) * char_w for line in lines), default=char_w)

    peak_scale = _EFFECT_SCALE_PEAK.get(template.effect, 1.02)
    border_px = max(1.0, template.outline * scale) * 2.0 + (4.0 * scale if template.effect == "box" else 0.0)
    box_w_px = min(avail_w, max_line_px * peak_scale + border_px)
    box_h_px = line_count * font_px * 1.18 + border_px + max(0.0, (peak_scale - 1.0) * font_px * 0.85)
    return (
        max(0.08, min(0.95, box_w_px / w)),
        max(0.03, min(0.55, box_h_px / h)),
    )


def _rect_intersection_area(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> float:
    ix0 = max(a[0], b[0])
    iy0 = max(a[1], b[1])
    ix1 = min(a[2], b[2])
    iy1 = min(a[3], b[3])
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    return (ix1 - ix0) * (iy1 - iy0)


def _extract_face_boxes(
    face_regions: list[FaceZone] | tuple[FaceZone, ...] | None,
    start: float,
    end: float,
) -> list[tuple[float, float, float, float]]:
    if not face_regions:
        return []
    padded: list[tuple[float, float, float, float]] = []
    for region in face_regions:
        if region.end < start - 0.05 or region.start > end + 0.05:
            continue
        for fx0, fy0, fx1, fy1 in region.boxes:
            padded.append((
                max(0.0, fx0 - 0.015),
                max(0.0, fy0 - 0.015),
                min(1.0, fx1 + 0.015),
                min(1.0, fy1 + 0.035),
            ))
    return padded


def resolve_caption_y_center(
    start: float,
    end: float,
    text: str,
    template: CaptionTemplate,
    width: int,
    height: int,
    *,
    y_ratio: float = DEFAULT_CAPTION_Y_RATIO,
    avoid_faces: bool = True,
    split_screen: bool = False,
    face_regions: list[FaceZone] | tuple[FaceZone, ...] | None = None,
    prev_y: float | None = None,
) -> tuple[float, bool]:
    """Compute stable, frame-safe normalized caption center Y [0..1] avoiding visible faces."""
    box_w, box_h = estimate_caption_bbox(text, template, width, height)
    half_h = box_h * 0.5
    min_bound = half_h + 0.025
    max_bound = max(min_bound, 1.0 - half_h - 0.025)

    using_default_y = abs(float(y_ratio) - DEFAULT_CAPTION_Y_RATIO) < 0.005
    raw_pref = 0.50 if (split_screen and using_default_y) else float(y_ratio)
    preferred_y = max(min_bound, min(max_bound, raw_pref))

    faces = _extract_face_boxes(face_regions, start, end) if avoid_faces else []
    if not faces:
        return preferred_y, False

    if template.alignment in (1, 4, 7) and not split_screen:
        cap_x0 = 0.065
        cap_x1 = min(0.935, cap_x0 + box_w)
    else:
        cap_x0 = max(0.04, 0.5 - box_w * 0.5)
        cap_x1 = min(0.96, 0.5 + box_w * 0.5)

    def overlap_at(cy: float) -> float:
        cap_rect = (cap_x0, cy - half_h, cap_x1, cy + half_h)
        return sum(_rect_intersection_area(cap_rect, face) for face in faces)

    pref_overlap = overlap_at(preferred_y)
    if pref_overlap <= 1e-4:
        if prev_y is not None and abs(preferred_y - prev_y) < 0.04:
            clamped_prev = max(min_bound, min(max_bound, prev_y))
            if overlap_at(clamped_prev) <= 1e-4:
                return clamped_prev, abs(clamped_prev - preferred_y) > 0.005
        return preferred_y, False

    is_portrait = height >= width
    safe_top = max(min_bound, (0.12 if is_portrait else 0.06) + half_h)
    safe_bot = min(max_bound, (0.91 if is_portrait else 0.94) - half_h)
    if safe_bot < safe_top:
        safe_top, safe_bot = min_bound, max_bound
    chrome_bot_edge = 0.80 if is_portrait else 0.91

    # Track horizontal-overlapping faces for "below speaker's face" preference
    horiz_faces = [f for f in faces if min(cap_x1, f[2]) - max(cap_x0, f[0]) > 0.02]
    if not horiz_faces:
        return preferred_y, False
    lowest_face_bottom = max(f[3] for f in horiz_faces)
    highest_face_top = min(f[1] for f in horiz_faces)

    candidates: list[float] = [preferred_y]
    if prev_y is not None:
        candidates.append(max(safe_top, min(safe_bot, prev_y)))
    for _, fy0, _, fy1 in horiz_faces:
        candidates.append(max(safe_top, min(safe_bot, fy1 + half_h + 0.008)))
        candidates.append(max(safe_top, min(safe_bot, fy0 - half_h - 0.012)))
    step_y = safe_top
    while step_y <= safe_bot + 1e-6:
        candidates.append(step_y)
        step_y += 0.02
    candidates.append(safe_bot)

    def candidate_cost(cy: float) -> float:
        ov = overlap_at(cy)
        # Prefer below face when avoiding overlap; penalize above-head jumps vs lower chest
        above_pen = 0.20 if (cy + half_h <= highest_face_top + 0.01 and lowest_face_bottom < safe_bot) else 0.0
        chrome_overhang = max(0.0, (cy + half_h) - chrome_bot_edge)
        dist_pen = abs(cy - preferred_y) * 1.15
        jump_pen = (abs(cy - prev_y) * 0.85) if prev_y is not None else 0.0
        return ov * 1000.0 + above_pen + chrome_overhang * 1.8 + dist_pen + jump_pen

    best_y = min(candidates, key=candidate_cost)
    if prev_y is not None and abs(best_y - prev_y) < 0.04:
        clamped_prev = max(safe_top, min(safe_bot, prev_y))
        if overlap_at(clamped_prev) <= overlap_at(best_y) + 1e-4:
            best_y = clamped_prev
    return best_y, abs(best_y - preferred_y) > 0.005


@dataclass(frozen=True, slots=True)
class CaptionTemplate:
    category: str
    description: str
    font: str
    size: int
    primary: str
    accent: str
    outline: float
    effect: str
    sample: str
    background: str
    uppercase: bool = False
    bold: bool = False
    border: int = 1
    shadow: int = 0
    alignment: int = 2
    words: int = 6


TEMPLATES = {
    "Impact": CaptionTemplate("Viral", "Heavy uppercase with a sharp entrance.", "Impact", 86,
        "#FFFFFF", "#FFD45A", 7, "punch", "MAKE IT COUNT", "#272033", uppercase=True, words=5),
    "Clean": CaptionTemplate("Minimal", "Soft fades and simple, readable type.", "Segoe UI", 68,
        "#F8F7FC", "#C7BEFF", 2, "fade", "Keep it simple.", "#1A2537"),
    "Neon": CaptionTemplate("Animated", "Electric purple with a pulsing glow.", "Arial Black", 75,
        "#F0CDFF", "#CA65FF", 3, "glow", "AFTER DARK", "#271739", uppercase=True, words=5),
    "Karaoke": CaptionTemplate("Animated", "A color sweep follows each spoken word.", "Trebuchet MS", 74,
        "#77FFE3", "#A3B3C9", 5, "karaoke", "Follow the rhythm", "#15313C", bold=True),
    "Hormozi": CaptionTemplate("Viral", "Bold yellow emphasis on the active word.", "Arial Black", 78,
        "#FBFBFD", "#FFF135", 8, "emphasis", "THE KEY IDEA", "#302A1E", uppercase=True, bold=True, words=5),
    "Pop Bounce": CaptionTemplate("Animated", "One word at a time with a springy overshoot.", "Arial Rounded MT Bold", 104,
        "#FFB9E9", "#DA65E1", 5, "bounce", "BOUNCE!", "#351E32", uppercase=True, bold=True, words=4),
    "Box Highlight": CaptionTemplate("Animated", "A purple block anchors the spoken word.", "Segoe UI", 85,
        "#FFFFFF", "#6A3EF1", 9, "box", "FOCUS", "#252143", uppercase=True, bold=True, border=3),
    "Cinematic": CaptionTemplate("Cinematic", "Warm serif type with a slow dissolve.", "Georgia", 77,
        "#FFEDC9", "#D0B48E", 3, "dissolve", "Set the scene.", "#30251F", shadow=3, words=7),
    "Gradient": CaptionTemplate("Animated", "A lilac-to-mint color ramp fades into view.", "Arial Black", 73,
        "#EE92FF", "#75FFDF", 5, "gradient", "COLOR IN MOTION", "#29223C", uppercase=True, words=5),
    "Typewriter": CaptionTemplate("Animated", "Monospace letters reveal as words are spoken.", "Consolas", 70,
        "#9FFF9D", "#D6FFB8", 4, "typewriter", "type it out_", "#1A2C2A", alignment=1, words=6),
    "Podcast": CaptionTemplate("Minimal", "Left-aligned conversational captions on a dark backing.", "Verdana", 60,
        "#F5F7FF", "#202738", 8, "podcast", "Let’s talk about it.", "#242C3D", border=3, alignment=1, words=8),
    "Viral Shorts": CaptionTemplate("Viral", "Quick rise, punchy scale, and alternating accents.", "Segoe UI Black", 86,
        "#FFF7F1", "#FF8A63", 7, "viral", "THIS CHANGES IT", "#38232D", uppercase=True, bold=True, words=4),
}
TEMPLATE_DEFAULT = "Impact"


@lru_cache(maxsize=1)
def _installed_fonts() -> set[str]:
    if os.name == "nt":
        import winreg

        fonts: set[str] = set()
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts") as key:
                    for index in range(winreg.QueryInfoKey(key)[1]):
                        name = winreg.EnumValue(key, index)[0]
                        fonts.add(name.split(" (", 1)[0].casefold())
            except OSError:
                continue
        return fonts

    fonts: set[str] = set()
    for directory in (
        Path("/System/Library/Fonts"),
        Path("/Library/Fonts"),
        Path.home() / "Library" / "Fonts",
        Path("/usr/share/fonts"),
    ):
        if not directory.is_dir():
            continue
        for item in directory.rglob("*"):
            if item.suffix.lower() in {".ttf", ".otf", ".ttc"}:
                fonts.add(item.stem.casefold())
    return fonts


def template_font(template: CaptionTemplate) -> str:
    fonts = _installed_fonts()
    if not fonts or template.font.casefold() in fonts:
        return template.font
    if os.name == "nt":
        fallback = {"Arial Rounded MT Bold": "Arial", "Segoe UI Black": "Segoe UI", "Georgia": "Times New Roman"}
        candidates = (fallback.get(template.font, "Arial"), "Segoe UI", "Arial")
    else:
        fallback = {
            "Segoe UI": "Helvetica Neue",
            "Segoe UI Black": "Arial Black",
            "Consolas": "Menlo",
            "Trebuchet MS": "Helvetica Neue",
            "Arial Rounded MT Bold": "Arial",
        }
        candidates = (fallback.get(template.font, "Helvetica Neue"), "Helvetica Neue", "Arial")
    for family in candidates:
        if family.casefold() in fonts:
            return family
    return "Helvetica Neue" if os.name != "nt" else "Arial"


def _cs(seconds: float) -> int:
    return round(max(0, seconds) * 100)


def ass_timestamp(seconds: float) -> str:
    minutes, hundredths = divmod(_cs(seconds), 6000)
    hours, minutes = divmod(minutes, 60)
    whole, fraction = divmod(hundredths, 100)
    return f"{hours}:{minutes:02}:{whole:02}.{fraction:02}"


def _escape(text: str) -> str:
    # libass treats an opening brace as markup. A zero-width space breaks control escapes.
    return (
        text.replace("\\", "\\\u200b")
        .replace("{", "\\{")
        .replace("}", "\\}")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\n", r"\N")
    )


def _ass_color(color: str, alpha: str = "") -> str:
    color = color.lstrip("#")
    return f"&H{alpha}{color[4:6]}{color[2:4]}{color[0:2]}&"


def _graphemes(text: str) -> list[str]:
    pieces: list[str] = []
    for char in text:
        if pieces and (unicodedata.category(char).startswith("M") or char == "\u200d" or pieces[-1].endswith("\u200d")):
            pieces[-1] += char
        else:
            pieces.append(char)
    return pieces


def _gradient(text: str, start: str, end: str) -> str:
    # Preserve contextual shaping for scripts with joining characters.
    latin = all(ord(char) < 0x370 for char in text)
    units = _graphemes(text) if latin else text.split()
    separator = "" if latin else " "
    first, last = bytes.fromhex(start[1:]), bytes.fromhex(end[1:])
    parts = []
    for index, unit in enumerate(units):
        weight = index / max(1, len(units) - 1)
        color = bytes(round(a + (b - a) * weight) for a, b in zip(first, last)).hex()
        parts.append(r"{\1c" + _ass_color(color) + "}" + _escape(unit))
    return separator.join(parts)


def _is_pure_punct(text: str) -> bool:
    return bool(text) and all(unicodedata.category(ch).startswith("P") for ch in text)


def _merge_compound_tokens(words: list[Word]) -> list[Word]:
    """Attach split hyphenated stems ('bener' + '-bener' / '-' + 'bener') and stray punctuation."""
    merged: list[Word] = []
    join_next = False
    for raw in words:
        text = raw.text.strip()
        if not text:
            continue
        if join_next and merged:
            prev = merged[-1]
            merged[-1] = Word(prev.start, max(prev.end, raw.end), prev.text + text)
            join_next = text[-1] in _HYPHEN_CHARS
            continue
        if merged:
            prev = merged[-1]
            gap = raw.start - prev.end
            if text in _HYPHEN_CHARS and gap <= 0.35:
                merged[-1] = Word(prev.start, max(prev.end, raw.end), prev.text + "-")
                join_next = True
                continue
            if (text[0] in _HYPHEN_CHARS or text[0] in "'’" or _is_pure_punct(text)) and gap <= 0.35:
                merged[-1] = Word(prev.start, max(prev.end, raw.end), prev.text + text)
                join_next = False
                continue
            if prev.text[-1] in _HYPHEN_CHARS and gap <= 0.25:
                merged[-1] = Word(prev.start, max(prev.end, raw.end), prev.text + text)
                join_next = text[-1] in _HYPHEN_CHARS
                continue
        merged.append(Word(raw.start, raw.end, text))
        join_next = False
    return merged


def _repair_isolated_zero_words(tokens: list[Word], seg_start: float, seg_end: float) -> list[Word]:
    """Give isolated zero-duration words monotonic non-zero bounds from neighboring slack."""
    repaired = [Word(w.start, w.end, w.text) for w in tokens]
    n = len(repaired)
    idx = 0
    while idx < n:
        if repaired[idx].end > repaired[idx].start:
            idx += 1
            continue
        run_end = idx
        while run_end + 1 < n and repaired[run_end + 1].end <= repaired[run_end + 1].start:
            run_end += 1
        count = run_end - idx + 1
        lower = repaired[idx - 1].end if idx > 0 else seg_start
        upper = repaired[run_end + 1].start if run_end + 1 < n else max(seg_end, lower + 0.12 * count)
        span = upper - lower
        if span >= 0.05 * count:
            step = min(0.24, span / count)
            for offset in range(count):
                s = lower + offset * step
                e = min(upper, s + step)
                repaired[idx + offset] = Word(s, max(s + 0.03, e), repaired[idx + offset].text)
        else:
            # Borrow tightly from the adjacent valid word when neighbors butt together.
            anchor_idx = run_end + 1 if run_end + 1 < n and repaired[run_end + 1].end - repaired[run_end + 1].start > 0.16 else idx - 1
            if 0 <= anchor_idx < n and repaired[anchor_idx].end - repaired[anchor_idx].start > 0.14:
                anchor = repaired[anchor_idx]
                borrow = min(0.08 * count, (anchor.end - anchor.start) * 0.38)
                step = borrow / count
                if anchor_idx > run_end:
                    anchor_new_start = anchor.start + borrow
                    repaired[anchor_idx] = Word(anchor_new_start, anchor.end, anchor.text)
                    base = anchor_new_start - borrow
                else:
                    anchor_new_end = anchor.end - borrow
                    repaired[anchor_idx] = Word(anchor.start, anchor_new_end, anchor.text)
                    base = anchor_new_end
                for offset in range(count):
                    s = base + offset * step
                    repaired[idx + offset] = Word(s, s + step, repaired[idx + offset].text)
            else:
                s = max(lower, repaired[idx].start)
                for offset in range(count):
                    cur_s = s + offset * 0.06
                    repaired[idx + offset] = Word(cur_s, cur_s + 0.06, repaired[idx + offset].text)
        idx = run_end + 1

    # Final strict monotonic cleanup inside segment tokens.
    cleaned: list[Word] = []
    for pos, item in enumerate(repaired):
        s = item.start
        e = item.end
        if cleaned:
            s = max(s, cleaned[-1].end)
        if pos + 1 < len(repaired):
            next_start = repaired[pos + 1].start
            if next_start > s + 0.04:
                e = min(e, next_start)
        if e <= s:
            if cleaned and not _is_pure_punct(item.text):
                prev = cleaned[-1]
                cleaned[-1] = Word(prev.start, max(prev.end, s + 0.03), f"{prev.text} {item.text}")
            continue
        cleaned.append(Word(s, e, item.text))
    return cleaned


def _valid_words_with_status(
    words: list[Word],
    highlight: Highlight,
    seg_start: float = 0.0,
    seg_end: float = 0.0,
    *,
    offset_sec: float = 0.0,
) -> tuple[list[Word], bool]:
    """Return (clipped_words, has_valid_word_timings).

    Distinguishes segments lacking word timestamps (falls back to segment text)
    from segments whose genuine word timestamps fell purely outside clip bounds
    or within <50ms edge bleed (skips off-clip text).
    """
    if not words:
        return [], False
    if any(not math.isfinite(word.start) or not math.isfinite(word.end) or not word.text.strip() for word in words):
        return [], False
    # Allow tiny Whisper jitter (<=50ms) but reject scrambled/unaligned word arrays.
    if any(current.start + 0.05 < previous.start for previous, current in zip(words, words[1:])):
        return [], False

    merged = _merge_compound_tokens(words)
    if not merged:
        return [], False
    zero_count = sum(1 for word in merged if word.end <= word.start)
    if zero_count == len(merged):
        return [], False
    if (len(merged) <= 2 and zero_count > 0) or (zero_count / len(merged) >= 0.35):
        return [], False

    fixed = _repair_isolated_zero_words(merged, seg_start or merged[0].start, seg_end or merged[-1].end)
    clip_dur = max(0.0, highlight.end - highlight.start)
    result: list[Word] = []
    for index, word in enumerate(fixed):
        orig_dur = max(0.0, word.end - word.start)
        rel_start = word.start - highlight.start + offset_sec
        rel_end = word.end - highlight.start + offset_sec
        if index + 1 < len(fixed):
            next_rel_start = fixed[index + 1].start - highlight.start + offset_sec
            rel_end = min(rel_end, next_rel_start)
        clamped_start = max(0.0, rel_start)
        clamped_end = min(clip_dur, rel_end)
        kept_dur = clamped_end - clamped_start
        # Drop tiny <50ms edge bleeds sliced at clip start/end when most of the word is outside the clip.
        if (rel_start < 0.0 or rel_end > clip_dur) and kept_dur < _EDGE_BLEED_MIN_S:
            if orig_dur > 0.12 or kept_dur / max(orig_dur, 1e-6) < 0.25:
                continue
        if result:
            clamped_start = max(clamped_start, result[-1].end)
        if _cs(clamped_end) > _cs(clamped_start):
            result.append(Word(clamped_start, clamped_end, word.text))
    return result, True


def _valid_words(
    words: list[Word],
    highlight: Highlight,
    seg_start: float = 0.0,
    seg_end: float = 0.0,
    *,
    offset_sec: float = 0.0,
) -> list[Word]:
    clipped, _ = _valid_words_with_status(words, highlight, seg_start, seg_end, offset_sec=offset_sec)
    return clipped


def _phrases(words: list[Word], maximum: int) -> list[list[Word]]:
    if not words:
        return []
    max_words = max(1, maximum)
    max_chars = 28 if max_words <= 5 else 34
    groups: list[list[Word]] = []
    for word in words:
        if not groups:
            groups.append([word])
            continue
        prev = groups[-1][-1]
        pause_gap = word.start - prev.end
        prev_char = prev.text[-1] if prev.text else ""
        curr_chars = sum(len(item.text) + 1 for item in groups[-1]) + len(word.text)
        curr_dur = prev.end - groups[-1][0].start
        should_break = (
            pause_gap > _MAX_WORD_GAP_GROUP_S
            or prev_char in _SENTENCE_END
            or (prev_char in _CLAUSE_END and (len(groups[-1]) >= 2 or curr_dur >= 0.55))
            or len(groups[-1]) >= max_words
            or curr_chars > max_chars
        )
        if should_break:
            groups.append([word])
        else:
            groups[-1].append(word)

    # Anti-orphan rebalance: eliminate accidental 5+1 tails when words belong to the same clause.
    if max_words >= 2 and len(groups) > 1:
        changed = True
        while changed:
            changed = False
            for idx in range(1, len(groups)):
                prev_group = groups[idx - 1]
                curr_group = groups[idx]
                if len(curr_group) != 1 or len(prev_group) < 3:
                    continue
                bridge_gap = curr_group[0].start - prev_group[-1].end
                boundary_char = prev_group[-1].text[-1] if prev_group[-1].text else ""
                if bridge_gap > _MAX_WORD_GAP_GROUP_S or boundary_char in _SENTENCE_END:
                    continue
                tail_word = prev_group[-1]
                moved_chars = len(tail_word.text) + 1 + len(curr_group[0].text)
                if moved_chars <= max_chars + 4 and not (tail_word.text[-1:] in _CLAUSE_END and len(prev_group) <= 3):
                    groups[idx - 1] = prev_group[:-1]
                    groups[idx] = [tail_word, curr_group[0]]
                    changed = True
                    break
    return groups


def _quantize_time(value: float, fps: float | None, clip_dur: float) -> float:
    clamped = max(0.0, min(clip_dur, value))
    if not fps or fps <= 0:
        return clamped
    snapped = round(clamped * fps) / fps
    return max(0.0, min(clip_dur, snapped))


def _normalize_cues(
    raw_cues: list[CaptionCue],
    clip_dur: float,
    profile: CaptionSyncProfile,
    *,
    fps: float | None = None,
) -> list[CaptionCue]:
    if not raw_cues or clip_dur <= 0:
        return []
    ordered = sorted(raw_cues, key=lambda item: (item.start, item.end))
    deduped: list[CaptionCue] = []
    for cue in ordered:
        start = max(0.0, min(clip_dur, cue.start))
        end = max(0.0, min(clip_dur, cue.end))
        if end <= start + 0.01:
            continue
        if deduped:
            prev = deduped[-1]
            # Drop completely redundant duplicate text overlapping inside previous cue.
            if cue.text == prev.text and start < prev.end + 0.05:
                merged_words = prev.words if len(prev.words) >= len(cue.words) else cue.words
                deduped[-1] = CaptionCue(prev.start, max(prev.end, end), merged_words, prev.text, prev.has_word_timing or cue.has_word_timing)
                continue
            if start < prev.end:
                start = prev.end
            if end <= start + 0.015:
                continue
        deduped.append(CaptionCue(start, end, cue.words, cue.text, cue.has_word_timing))

    normalized: list[CaptionCue] = []
    for idx, cue in enumerate(deduped):
        prev_end = normalized[-1].end if normalized else 0.0
        next_start = deduped[idx + 1].start if idx + 1 < len(deduped) else clip_dur

        apply_lead = profile.lead_in_s > 0.0
        apply_tail = cue.has_word_timing or profile.trailing_hold_s > 0.08
        lead_in = profile.lead_in_s if apply_lead else 0.0
        new_start = max(prev_end, cue.start - lead_in)

        avail_after = max(0.0, next_start - cue.end)
        tail_pad = min(profile.trailing_hold_s, avail_after) if apply_tail else 0.0
        new_end = min(next_start, cue.end + tail_pad)

        # Ensure crisp minimum display duration within surrounding silence without pushing into next spoken phrase.
        if new_end - new_start < profile.min_duration_s and (cue.has_word_timing or cue.end - cue.start < 0.35):
            needed = profile.min_duration_s - (new_end - new_start)
            extra_cap = max(0.0, cue.end + profile.max_hold_extension_s - new_end)
            extend = min(needed, max(0.0, next_start - new_end), extra_cap)
            new_end += extend

        new_start = max(0.0, min(clip_dur, new_start))
        new_end = max(new_start, min(clip_dur, new_end))
        if _cs(new_end) <= _cs(new_start):
            continue

        words = list(cue.words)
        if words:
            w_list: list[Word] = []
            for w_idx, word in enumerate(words):
                ws = new_start if w_idx == 0 else max(new_start, word.start, w_list[-1].end)
                we = new_end if w_idx == len(words) - 1 else min(new_end, word.end)
                if we > ws + 0.01:
                    w_list.append(Word(ws, we, word.text))
            if w_list:
                w_list[-1] = Word(w_list[-1].start, max(w_list[-1].start + 0.01, new_end), w_list[-1].text)
            words = w_list
        normalized.append(CaptionCue(new_start, new_end, tuple(words), cue.text, cue.has_word_timing))

    # Close micro-gaps (<35-50ms, ~1 frame) so consecutive captions never blink blank between cues.
    for idx in range(len(normalized) - 1):
        curr = normalized[idx]
        nxt = normalized[idx + 1]
        gap = nxt.start - curr.end
        if 0.0 < gap <= profile.bridge_cue_gap_s:
            words = curr.words
            if words:
                words = (*words[:-1], Word(words[-1].start, nxt.start, words[-1].text))
            normalized[idx] = CaptionCue(curr.start, nxt.start, words, curr.text, curr.has_word_timing)

    if fps and fps > 0:
        frame_min = max(0.02, 1.0 / fps)
        quantized: list[CaptionCue] = []
        for idx, cue in enumerate(normalized):
            prev_end = quantized[-1].end if quantized else 0.0
            q_start = max(prev_end, _quantize_time(cue.start, fps, clip_dur))
            q_end = _quantize_time(cue.end, fps, clip_dur)
            if q_end <= q_start + 1e-4:
                q_end = min(clip_dur, q_start + frame_min)
            if _cs(q_end) <= _cs(q_start):
                continue
            q_words: list[Word] = []
            if cue.words:
                for w_idx, word in enumerate(cue.words):
                    ws = q_start if w_idx == 0 else max(q_words[-1].end, _quantize_time(word.start, fps, clip_dur))
                    if w_idx == len(cue.words) - 1:
                        we = q_end
                    else:
                        we = min(q_end, _quantize_time(word.end, fps, clip_dur))
                        if we <= ws + 1e-4:
                            we = min(q_end, ws + frame_min)
                    if _cs(we) > _cs(ws):
                        q_words.append(Word(ws, we, word.text))
                if q_words:
                    q_words[-1] = Word(q_words[-1].start, q_end, q_words[-1].text)
            quantized.append(CaptionCue(q_start, q_end, tuple(q_words), cue.text, cue.has_word_timing))
        return quantized
    return normalized


def build_caption_cues(
    transcript: Transcript,
    highlight: Highlight,
    template: CaptionTemplate,
    *,
    fps: float | None = None,
    sync_mode: str = "precise",
    offset_ms: int = 0,
) -> list[CaptionCue]:
    profile = _SYNC_PROFILES.get(str(sync_mode).lower(), _SYNC_PROFILES["precise"])
    offset_sec = max(-500, min(500, int(offset_ms))) / 1000.0
    clip_dur = max(0.0, highlight.end - highlight.start)
    raw_cues: list[CaptionCue] = []
    for segment in transcript.segments:
        if segment.end <= highlight.start or segment.start >= highlight.end:
            continue
        words, used_word_timing = _valid_words_with_status(
            segment.words, highlight, segment.start, segment.end, offset_sec=offset_sec,
        )
        if used_word_timing:
            if not words:
                continue
            for grp in _phrases(words, template.words):
                text = " ".join(item.text for item in grp)
                raw_cues.append(CaptionCue(grp[0].start, grp[-1].end, tuple(grp), text, True))
        else:
            raw_start = segment.start - highlight.start + offset_sec
            raw_end = segment.end - highlight.start + offset_sec
            start = max(0.0, raw_start)
            end = min(clip_dur, raw_end)
            # Guard un-worded SRT boundaries against <80ms tail bleed from off-clip sentences.
            if (raw_start < -0.25 or raw_end > clip_dur + 0.25) and (end - start) < 0.08:
                continue
            if _cs(end) > _cs(start) and segment.text.strip():
                raw_cues.append(CaptionCue(start, end, (), segment.text.strip(), False))
    return _normalize_cues(raw_cues, clip_dur, profile, fps=fps)


class _CaptionScript:
    def __init__(
        self,
        template: CaptionTemplate,
        width: int,
        height: int,
        *,
        split_screen: bool = False,
        y_ratio: float = DEFAULT_CAPTION_Y_RATIO,
        avoid_faces: bool = True,
        face_regions: list[FaceZone] | tuple[FaceZone, ...] | None = None,
    ) -> None:
        self.template = template
        self.width = width
        self.height = height
        self.split_screen = split_screen
        self.y_ratio = max(CAPTION_Y_MIN, min(CAPTION_Y_MAX, float(y_ratio)))
        self.avoid_faces = avoid_faces
        self.face_regions = face_regions
        self.events: list[tuple[int, int, int, str]] = []
        self.scale = min(width / 1080, height / 1080)
        self._prev_y: float | None = None

        _, rep_box_h = estimate_caption_bbox(template.sample or "Sample line", template, width, height)
        rep_h_px = rep_box_h * height
        if split_screen:
            self.margin = 0
        else:
            bottom_px = min(height - 10.0, max(rep_h_px + 10.0, self.y_ratio * height + rep_h_px * 0.5))
            self.margin = max(10, round(height - bottom_px))

    def _cue_placement(self, start: float, end: float, text: str) -> tuple[int, int, int | None]:
        """Return (event_margin_v_override, target_bottom_y_px, split_pos_center_y_px_or_None)."""
        _, box_h = estimate_caption_bbox(text, self.template, self.width, self.height)
        resolved_y, _ = resolve_caption_y_center(
            start, end, text, self.template, self.width, self.height,
            y_ratio=self.y_ratio,
            avoid_faces=self.avoid_faces,
            split_screen=self.split_screen,
            face_regions=self.face_regions,
            prev_y=self._prev_y,
        )
        self._prev_y = resolved_y
        box_h_px = box_h * self.height
        bottom_px = round(min(self.height - 10.0, max(box_h_px + 10.0, resolved_y * self.height + box_h_px * 0.5)))
        if self.split_screen:
            center_px = round(resolved_y * self.height)
            if abs(resolved_y - 0.50) > 0.025:
                return 0, bottom_px, center_px
            return 0, self.height // 2, None
        cue_margin_v = max(10, self.height - bottom_px)
        override = cue_margin_v if abs(cue_margin_v - self.margin) > 2 else 0
        return override, bottom_px, None

    def add(self, start: float, end: float, text: str, tags: str = "", *, layer: int = 0, margin_v: int = 0) -> None:
        start_cs, end_cs = _cs(start), _cs(end)
        if end_cs > start_cs:
            override = f"{{{tags}}}" if tags else ""
            row = f"Dialogue: {layer},{ass_timestamp(start)},{ass_timestamp(end)},Default,,0,0,{margin_v},," + override + text
            self.events.append((layer, start_cs, end_cs, row))

    def transform_text(self, text: str) -> str:
        return text.upper() if self.template.uppercase else text

    def fit(self, text: str) -> str:
        longest = max((len(word) for word in text.split()), default=1)
        scale = max(.42, min(1.0, 18 / longest))
        return fr"\fs{max(12, round(self.template.size * self.scale * scale))}" if scale < 1 else ""

    def phrase(
        self,
        start: float,
        end: float,
        text: str,
        words: list[Word] | None = None,
        *,
        margin_v: int = 0,
        split_pos_y: int | None = None,
    ) -> None:
        style = self.template
        text = self.transform_text(text)
        body = _escape(text)
        fit = self.fit(text)
        pos_tag = fr"\an5\pos({self.width // 2},{split_pos_y})" if split_pos_y is not None else ""
        length = max(1, round((end - start) * 1000))
        short_fade = min(140, max(25, length // 4))
        effect = style.effect
        if effect == "punch":
            if length >= 260:
                half, full = 105, 210
            else:
                full = max(65, round(length * 0.68))
                half = max(32, full // 2)
            tags = fr"\fscx82\fscy82\t(0,{half},\fscx111\fscy111)\t({half},{full},\fscx100\fscy100)"
        elif effect == "dissolve":
            fade = min(300, max(40, length // 3))
            tags = fr"\fsp{round(self.scale * 4)}\fad({fade},{fade})"
        elif effect == "podcast":
            tags = fr"\fad({min(90, short_fade)},0)"
        else:
            tags = fr"\fad({short_fade},{short_fade})"

        if effect == "glow":
            border = max(2, round(16 * self.scale))
            glow_peak = min(160, max(50, length // 3))
            glow_fade = min(460, max(glow_peak + 60, length))
            glow = (fr"\bord{border}\blur{max(1, round(10 * self.scale))}\3c{_ass_color(style.accent)}"
                    fr"\1a&HFF&\3a&H92&\t(0,{glow_peak},\3a&H15&)\t({glow_peak},{glow_fade},\3a&H8C&)")
            self.add(start, end, body, fit + pos_tag + glow, margin_v=margin_v)
            self.add(start, end, body, fit + pos_tag + tags, layer=1, margin_v=margin_v)
            return
        if effect == "gradient":
            body = _gradient(text, style.primary, style.accent)
            track_ms = min(280, max(90, length // 2))
            tags += fr"\fsp{max(1, round(self.scale * 6))}\t(0,{track_ms},\fsp0)"
        if effect == "karaoke" and words:
            body = " ".join(
                fr"{{\kt{max(0, _cs(word.start) - _cs(start))}\kf{max(1, _cs(word.end) - _cs(word.start))}}}"
                + _escape(self.transform_text(word.text)) for word in words
            )
        if effect == "karaoke" and not words:
            tags += r"\1c&H00F4F4F4&"
        self.add(start, end, body, fit + pos_tag + tags, margin_v=margin_v)

    def _base_y(self) -> int:
        if self.split_screen:
            return self.height // 2
        if self.template.alignment in (7, 8, 9):
            return self.margin
        return self.height - self.margin

    def group(self, cue: CaptionCue, profile: CaptionSyncProfile) -> None:
        style = self.template
        words = list(cue.words)
        cue_margin_v, cue_bottom_px, split_pos_y = self._cue_placement(cue.start, cue.end, cue.text)
        if not words or not cue.has_word_timing:
            self.phrase(cue.start, cue.end, cue.text, None, margin_v=cue_margin_v, split_pos_y=split_pos_y)
            return
        if style.effect not in {"emphasis", "viral", "bounce", "box", "typewriter"}:
            self.phrase(
                cue.start, cue.end, " ".join(item.text for item in words), words,
                margin_v=cue_margin_v, split_pos_y=split_pos_y,
            )
            return

        full_text = self.transform_text(" ".join(item.text for item in words))
        plain = [_escape(self.transform_text(word.text)) for word in words]
        fit = self.fit(full_text)
        background = fr"\bord1\3c{_ass_color('#13101C')}"
        before = ""
        base_y = split_pos_y if split_pos_y is not None else (self.height // 2 if self.split_screen else cue_bottom_px)
        pos_tag = fr"\an5\pos({self.width // 2},{split_pos_y})" if split_pos_y is not None else ""

        # Precompute word spans, seamlessly bridging tiny <=140-180ms gaps inside one phrase.
        spans: list[tuple[float, float]] = []
        for idx, word in enumerate(words):
            next_start = words[idx + 1].start if idx + 1 < len(words) else cue.end
            if 0.0 < next_start - word.end <= profile.bridge_word_gap_s:
                spans.append((word.start, next_start))
            elif idx == len(words) - 1 and cue.end > word.end:
                spans.append((word.start, cue.end))
            else:
                spans.append((word.start, word.end))

        cursor = cue.start
        for index, word in enumerate(words):
            w_start, w_end = spans[index]
            word_text = self.transform_text(word.text)
            length = max(1, round((w_end - w_start) * 1000))
            if style.effect in {"emphasis", "viral"}:
                if _cs(w_start) > _cs(cursor):
                    self.add(cursor, w_start, " ".join(plain), fit + pos_tag, margin_v=cue_margin_v)
                accent = "#A2FFE8" if style.effect == "viral" and index % 2 else style.accent
                emphasis = fr"\1c{_ass_color(accent)}\b1\fscy112"
                movement = pos_tag
                if style.effect == "viral":
                    if index == 0:
                        move_ms = min(115, length)
                        rise_y = base_y + round(16 * self.scale)
                        an_prefix = r"\an5" if self.split_screen else ""
                        movement = fr"{an_prefix}\move({self.width // 2},{rise_y},{self.width // 2},{base_y},0,{move_ms})"
                        emphasis += fr"\fscy122\t(0,{min(125, length)},\fscy100)"
                    else:
                        emphasis = fr"\1c{_ass_color(accent)}\b1\fscy114\t(0,{min(100, length)},\fscy104)"
                line = plain.copy()
                line[index] = "{" + emphasis + "}" + plain[index] + r"{\1c\fscy100}"
                self.add(w_start, w_end, " ".join(line), fit + movement, margin_v=cue_margin_v)
            elif style.effect == "bounce":
                word_fit = self.fit(word_text)
                if length >= 220:
                    first, second, last = [max(1, round(length * f)) for f in (.28, .55, .82)]
                    peak = 122
                else:
                    first, second, last = [max(1, round(length * f)) for f in (.30, .62, .90)]
                    peak = 114
                self.add(w_start, w_end, _escape(word_text), word_fit + pos_tag + (
                    fr"\fscx64\fscy64\t(0,{first},\fscx{peak}\fscy{peak})"
                    fr"\t({first},{second},\fscx94\fscy94)\t({second},{last},\fscx100\fscy100)"
                ), margin_v=cue_margin_v)
            elif style.effect == "box":
                if _cs(w_start) > _cs(cursor):
                    self.add(cursor, w_start, " ".join(plain), fit + pos_tag + background, margin_v=cue_margin_v)
                box = max(2, round(style.outline * self.scale))
                reveal = fr"\bord{box}\3c\3a&H65&\t(0,{min(95, length)},\3a&H00&)"
                line = plain.copy()
                line[index] = "{" + reveal + "}" + line[index] + "{" + background + r"\3a&H00&}"
                self.add(w_start, w_end, " ".join(line), fit + pos_tag + background, margin_v=cue_margin_v)
            else:
                if before and _cs(w_start) > _cs(cursor):
                    self.add(cursor, w_start, _escape(before.rstrip()), fit + pos_tag, margin_v=cue_margin_v)
                chars = _graphemes(word_text)
                active_window = max(0.02, (word.end - w_start) * .82)
                stride = active_window / max(1, len(chars))
                for count in range(1, len(chars) + 1):
                    c_start = w_start + (count - 1) * stride
                    c_end = c_start + stride if count < len(chars) else w_end
                    body = before + "".join(chars[:count])
                    self.add(c_start, c_end, _escape(body), fit + pos_tag, margin_v=cue_margin_v)
                before += word_text + " "
            cursor = w_end

    def write(self, output: Path) -> None:
        style = self.template
        outline = style.accent if style.effect in {"box", "podcast"} else "#13101C"
        secondary = style.accent if style.effect == "karaoke" else style.primary
        margin_x = max(12, round(self.width * .065))
        alignment = 5 if self.split_screen else style.alignment
        row = (
            f"Default,{template_font(style)},{max(12, round(style.size*self.scale))},"
            f"{_ass_color(style.primary, '00')},{_ass_color(secondary, '00')},"
            f"{_ass_color(outline, '00')},&H5013101C,"
            f"{-int(style.bold)},0,0,0,100,100,0,0,{style.border},"
            f"{max(1, round(style.outline*self.scale))},{round(style.shadow*self.scale)},"
            f"{alignment},{margin_x},{margin_x},{self.margin},-1"
        )
        header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {self.width}
PlayResY: {self.height}
ScaledBorderAndShadow: yes
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: {row}

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
        ordered = [row_str for _, _, _, row_str in sorted(self.events, key=lambda item: (item[0], item[1], item[2]))]
        output.write_text(header + "\n".join(ordered) + "\n", encoding="utf-8-sig")


def write_ass(
    transcript: Transcript,
    highlight: Highlight,
    output: Path,
    resolution: tuple[int, int],
    template_name: str,
    *,
    split_screen: bool = False,
    fps: float | None = None,
    sync_mode: str = "precise",
    offset_ms: int = 0,
    y_ratio: float = DEFAULT_CAPTION_Y_RATIO,
    avoid_faces: bool = True,
    face_regions: list[FaceZone] | tuple[FaceZone, ...] | None = None,
) -> None:
    template = TEMPLATES.get(template_name, TEMPLATES[TEMPLATE_DEFAULT])
    profile = _SYNC_PROFILES.get(str(sync_mode).lower(), _SYNC_PROFILES["precise"])
    script = _CaptionScript(
        template,
        *resolution,
        split_screen=split_screen,
        y_ratio=y_ratio,
        avoid_faces=avoid_faces,
        face_regions=face_regions,
    )
    for cue in build_caption_cues(transcript, highlight, template, fps=fps, sync_mode=sync_mode, offset_ms=offset_ms):
        script.group(cue, profile)
    script.write(output)
