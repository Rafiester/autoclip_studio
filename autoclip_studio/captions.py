from __future__ import annotations

import math
import os
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .models import Highlight, Transcript, Word


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


def _valid_words(words: list[Word], highlight: Highlight) -> list[Word]:
    if any(not math.isfinite(word.start) or not math.isfinite(word.end) or word.end <= word.start or
           not word.text.strip() for word in words):
        return []
    if any(current.start < previous.start for previous, current in zip(words, words[1:])):
        return []
    result = []
    for index, word in enumerate(words):
        start = max(word.start, highlight.start)
        end = min(word.end, highlight.end)
        if index + 1 < len(words):
            end = min(end, words[index + 1].start)
        if _cs(end - highlight.start) > _cs(start - highlight.start):
            result.append(Word(start - highlight.start, end - highlight.start, word.text.strip()))
    return result


def _phrases(words: list[Word], maximum: int) -> list[list[Word]]:
    groups: list[list[Word]] = []
    for word in words:
        if (not groups or len(groups[-1]) >= maximum or word.start - groups[-1][-1].end > .6
                or sum(len(item.text) + 1 for item in groups[-1]) + len(word.text) > 34):
            groups.append([])
        groups[-1].append(word)
    return groups


class _CaptionScript:
    def __init__(self, template: CaptionTemplate, width: int, height: int) -> None:
        self.template = template
        self.width = width
        self.height = height
        self.events: list[str] = []
        self.scale = min(width / 1080, height / 1080)
        self.margin = round(height * .14)

    def add(self, start: float, end: float, text: str, tags: str = "", *, layer: int = 0) -> None:
        if _cs(end) > _cs(start):
            override = f"{{{tags}}}" if tags else ""
            self.events.append(f"Dialogue: {layer},{ass_timestamp(start)},{ass_timestamp(end)},Default,,0,0,0,,"
                               + override + text)

    def transform_text(self, text: str) -> str:
        return text.upper() if self.template.uppercase else text

    def fit(self, text: str) -> str:
        longest = max((len(word) for word in text.split()), default=1)
        scale = max(.42, min(1.0, 18 / longest))
        return fr"\fs{max(12, round(self.template.size * self.scale * scale))}" if scale < 1 else ""

    def phrase(self, start: float, end: float, text: str, words: list[Word] | None = None) -> None:
        style = self.template
        text = self.transform_text(text)
        body = _escape(text)
        fit = self.fit(text)
        length = round((end - start) * 1000)
        short_fade = min(140, length // 4)
        effect = style.effect
        if effect == "punch":
            tags = r"\fscx82\fscy82\t(0,105,\fscx111\fscy111)\t(105,210,\fscx100\fscy100)"
        elif effect == "dissolve":
            fade = min(300, length // 3)
            tags = fr"\fsp{round(self.scale * 4)}\fad({fade},{fade})"
        elif effect == "podcast":
            tags = fr"\fad({min(90, short_fade)},0)"
        else:
            tags = fr"\fad({short_fade},{short_fade})"

        if effect == "glow":
            border = max(2, round(16 * self.scale))
            glow = (fr"\bord{border}\blur{max(1, round(10 * self.scale))}\3c{_ass_color(style.accent)}"
                    r"\1a&HFF&\3a&H92&\t(0,160,\3a&H15&)\t(160,460,\3a&H8C&)")
            self.add(start, end, body, fit + glow)
            self.add(start, end, body, fit + tags, layer=1)
            return
        if effect == "gradient":
            body = _gradient(text, style.primary, style.accent)
            tags += fr"\fsp{max(1, round(self.scale * 6))}\t(0,280,\fsp0)"
        if effect == "karaoke" and words:
            body = " ".join(
                fr"{{\kt{_cs(word.start) - _cs(start)}\kf{_cs(word.end) - _cs(word.start)}}}"
                + _escape(self.transform_text(word.text)) for word in words
            )
        if effect == "karaoke" and not words:
            tags += r"\1c&H00F4F4F4&"
        self.add(start, end, body, fit + tags)

    def group(self, words: list[Word]) -> None:
        style = self.template
        text = " ".join(item.text for item in words)
        if style.effect not in {"emphasis", "viral", "bounce", "box", "typewriter"}:
            self.phrase(words[0].start, words[-1].end, text, words)
            return
        text = self.transform_text(text)
        plain = [_escape(self.transform_text(word.text)) for word in words]
        fit = self.fit(text)
        background = fr"\bord1\3c{_ass_color('#13101C')}"
        before = ""
        cursor = words[0].start
        for index, word in enumerate(words):
            text = self.transform_text(word.text)
            length = max(1, round((word.end - word.start) * 1000))
            if style.effect in {"emphasis", "viral"}:
                self.add(cursor, word.start, " ".join(plain), fit)
                accent = "#A2FFE8" if style.effect == "viral" and index % 2 else style.accent
                emphasis = fr"\1c{_ass_color(accent)}\b1\fscy112"
                movement = ""
                if style.effect == "viral":
                    y = self.height - self.margin
                    movement = fr"\move({self.width//2},{y+round(20*self.scale)},{self.width//2},{y},0,{min(110,length)})"
                    emphasis += fr"\fscy138\t(0,{min(130,length)},\fscy100)"
                line = plain.copy()
                line[index] = "{" + emphasis + "}" + plain[index] + r"{\1c\fscy100}"
                self.add(word.start, word.end, " ".join(line), fit + movement)
            elif style.effect == "bounce":
                first, second, last = [max(1, round(length * fraction)) for fraction in (.28, .55, .82)]
                self.add(word.start, word.end, _escape(text), self.fit(text) + (
                    fr"\fscx64\fscy64\t(0,{first},\fscx122\fscy122)"
                    fr"\t({first},{second},\fscx94\fscy94)\t({second},{last},\fscx100\fscy100)"
                ))
            elif style.effect == "box":
                self.add(cursor, word.start, " ".join(plain), fit + background)
                box = max(2, round(style.outline * self.scale))
                reveal = fr"\bord{box}\3c\3a&H65&\t(0,{min(95, length)},\3a&H00&)"
                line = plain.copy()
                line[index] = "{" + reveal + "}" + line[index] + "{" + background + r"\3a&H00&}"
                self.add(word.start, word.end, " ".join(line), fit + background)
            else:
                if before:
                    self.add(cursor, word.start, _escape(before.rstrip()))
                chars = _graphemes(text)
                stride = (word.end - word.start) * .82 / max(1, len(chars))
                for count in range(1, len(chars) + 1):
                    start = word.start + (count - 1) * stride
                    end = start + stride if count < len(chars) else word.end
                    body = before + "".join(chars[:count])
                    self.add(start, end, _escape(body), self.fit(body))
                before += text + " "
            cursor = word.end

    def write(self, output: Path) -> None:
        style = self.template
        outline = style.accent if style.effect in {"box", "podcast"} else "#13101C"
        secondary = style.accent if style.effect == "karaoke" else style.primary
        margin_x = max(12, round(self.width * .065))
        row = (
            f"Default,{template_font(style)},{max(12, round(style.size*self.scale))},"
            f"{_ass_color(style.primary, '00')},{_ass_color(secondary, '00')},"
            f"{_ass_color(outline, '00')},&H5013101C,"
            f"{-int(style.bold)},0,0,0,100,100,0,0,{style.border},"
            f"{max(1, round(style.outline*self.scale))},{round(style.shadow*self.scale)},"
            f"{style.alignment},{margin_x},{margin_x},{self.margin},-1"
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
        output.write_text(header + "\n".join(self.events) + "\n", encoding="utf-8-sig")


def write_ass(transcript: Transcript, highlight: Highlight, output: Path,
              resolution: tuple[int, int], template_name: str) -> None:
    template = TEMPLATES.get(template_name, TEMPLATES[TEMPLATE_DEFAULT])
    script = _CaptionScript(template, *resolution)
    for segment in transcript.segments:
        if segment.end <= highlight.start or segment.start >= highlight.end:
            continue
        words = _valid_words(segment.words, highlight)
        if words:
            for group in _phrases(words, template.words):
                script.group(group)
        else:
            start = max(0, segment.start - highlight.start)
            end = min(highlight.end, segment.end) - highlight.start
            script.phrase(start, end, segment.text)
    script.write(output)
