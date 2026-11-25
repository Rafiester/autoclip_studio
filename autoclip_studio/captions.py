from __future__ import annotations

from pathlib import Path

from .models import Highlight, Transcript, Word


TEMPLATES = {
    "Impact": {"font": "Arial", "size": 74, "primary": "&H00FFFFFF", "accent": "&H0000D7FF", "outline": 7},
    "Clean": {"font": "Segoe UI Semibold", "size": 62, "primary": "&H00FFFFFF", "accent": "&H00FFFFFF", "outline": 4},
    "Neon": {"font": "Arial Black", "size": 68, "primary": "&H00FFFFFF", "accent": "&H00FF70FF", "outline": 6},
}


def ass_timestamp(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hours = int(seconds // 3600)
    minutes = int(seconds % 3600 // 60)
    secs = seconds % 60
    return f"{hours}:{minutes:02d}:{secs:05.2f}"


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}").replace("\n", " ")


def write_ass(transcript: Transcript, highlight: Highlight, output: Path,
              resolution: tuple[int, int], template_name: str) -> None:
    width, height = resolution
    style = TEMPLATES.get(template_name, TEMPLATES["Impact"])
    words: list[Word] = []
    events: list[str] = []
    for segment in transcript.segments:
        if segment.end < highlight.start or segment.start > highlight.end:
            continue
        if segment.words:
            words.extend(segment.words)
        else:
            start = max(0, segment.start - highlight.start)
            end = min(highlight.end, segment.end) - highlight.start
            if end > start:
                events.append(f"Dialogue: 0,{ass_timestamp(start)},{ass_timestamp(end)},Default,,0,0,0,,"
                              + _escape(segment.text.upper()))
    words = [word for word in words if word.end >= highlight.start and word.start <= highlight.end]
    for index in range(0, len(words), 5):
        group = words[index:index + 5]
        start = max(0, group[0].start - highlight.start)
        end = min(highlight.end - highlight.start, group[-1].end - highlight.start + 0.08)
        karaoke = []
        for word in group:
            centis = max(1, round((word.end - word.start) * 100))
            karaoke.append(f"{{\\kf{centis}}}{_escape(word.text.upper())}")
        events.append(
            f"Dialogue: 0,{ass_timestamp(start)},{ass_timestamp(end)},Default,,0,0,0,,"
            + " ".join(karaoke)
        )
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
ScaledBorderAndShadow: yes
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{style['font']},{style['size']},{style['primary']},{style['accent']},&H00101010,&H60000000,-1,0,0,0,100,100,1,0,1,{style['outline']},1,2,70,70,{round(height * .14)},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    output.write_text(header + "\n".join(events) + "\n", encoding="utf-8-sig")

