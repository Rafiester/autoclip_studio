from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class Word:
    start: float
    end: float
    text: str

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Word":
        return cls(float(value["start"]), float(value["end"]), str(value["text"]))


@dataclass(slots=True)
class Segment:
    start: float
    end: float
    text: str
    words: list[Word] = field(default_factory=list)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Segment":
        return cls(
            float(value["start"]), float(value["end"]), str(value["text"]),
            [Word.from_dict(item) for item in value.get("words", [])],
        )


@dataclass(slots=True)
class Transcript:
    duration: float
    language: str
    segments: list[Segment]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Transcript":
        return cls(
            float(value.get("duration", 0)), str(value.get("language", "unknown")),
            [Segment.from_dict(item) for item in value.get("segments", [])],
        )


@dataclass(slots=True)
class Highlight:
    start: float
    end: float
    title: str
    hook: str
    description: str
    score: int
    reason: str = ""
    ai_score: int | None = None
    replay_score: int | None = None
    detection_mode: str = "ai"

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Highlight":
        score_val = max(0, min(100, int(float(value.get("score", 50)))))
        raw_ai = value.get("ai_score")
        raw_replay = value.get("replay_score")
        ai_val = max(0, min(100, int(float(raw_ai)))) if raw_ai is not None else None
        replay_val = max(0, min(100, int(float(raw_replay)))) if raw_replay is not None else None
        return cls(
            float(value.get("start", value.get("start_time", 0))),
            float(value.get("end", value.get("end_time", 0))),
            str(value.get("title", "Untitled clip")),
            str(value.get("hook", value.get("hook_sentence", ""))),
            str(value.get("description", "")),
            score_val,
            str(value.get("reason", value.get("virality_reason", ""))),
            ai_score=ai_val,
            replay_score=replay_val,
            detection_mode=str(value.get("detection_mode") or "ai"),
        )


@dataclass(slots=True)
class ProjectOptions:
    source: str
    clip_count: int = 3
    min_duration: int = 25
    max_duration: int = 60
    aspect_ratio: str = "9:16"
    resolution: str = "1080x1920"
    face_tracking: bool = True
    reframe_mode: str = "auto"
    reframe_style: str = "natural"
    captions: bool = True
    caption_template: str = "Impact"
    provider: str = "gemini"
    model: str = "gemini-2.5-flash"
    multimodal: bool = False
    cache_enabled: bool = True
    whisper_model: str = "small"
    whisper_device: str = "auto"
    language: str | None = None
    output_dir: str = "output"
    project_name: str = ""
    youtube_metadata: dict[str, Any] | None = None
    srt_path: str = ""
    srt_offset_seconds: float = 0.0
    highlight_mode: str = "ai"
    ai_weight: float = 0.6
    use_youtube_subtitles: bool = False
    youtube_subtitle_language: str = ""

    @property
    def output_size(self) -> tuple[int, int]:
        try:
            width, height = self.resolution.lower().split("x", 1)
            return int(width), int(height)
        except (TypeError, ValueError):
            return (1080, 1920)


@dataclass(slots=True)
class RenderedClip:
    path: Path
    highlight: Highlight
    duration: float

