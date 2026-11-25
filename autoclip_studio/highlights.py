from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Callable

from .models import Highlight, Segment, Transcript
from .providers import AIProvider, ProviderError


SYSTEM_PROMPT = """You are an expert short-form video editor. Select contextually complete,
high-retention excerpts from a timestamped transcript. Favor immediate hooks, useful insights,
emotion, surprising claims, story turns, and quotable conclusions. Never invent timestamps or
content. Return only a JSON object with a `highlights` array. Each item must contain start, end,
title, hook, description, score (0-100), and reason."""


def transcript_text(segments: list[Segment]) -> str:
    return "\n".join(f"[{item.start:.1f}-{item.end:.1f}] {item.text}" for item in segments)


def chunk_transcript(transcript: Transcript, chunk_seconds: int = 1200,
                     overlap_seconds: int = 60) -> list[list[Segment]]:
    if not transcript.segments:
        return []
    chunks: list[list[Segment]] = []
    start = 0.0
    while start < transcript.duration:
        end = start + chunk_seconds
        chunk = [item for item in transcript.segments if item.end >= start and item.start <= end]
        if chunk:
            chunks.append(chunk)
        if end >= transcript.duration:
            break
        start += max(1, chunk_seconds - overlap_seconds)
    return chunks


def select_highlights(transcript: Transcript, count: int, min_duration: int,
                      max_duration: int, provider: AIProvider | None,
                      log: Callable[[str], None], video_path: Path | None = None,
                      multimodal: bool = False) -> list[Highlight]:
    candidates: list[Highlight] = []
    if multimodal and provider and provider.kind == "gemini" and video_path:
        log("Uploading source to Gemini for optional multimodal analysis")
        video_prompt = (
            f"Analyze this video and select up to {count + 2} contextually complete highlights "
            f"lasting {min_duration}-{max_duration} seconds. Use timestamps from the video. "
            "Return a JSON object with a highlights array containing start, end, title, hook, "
            "description, score, and reason."
        )
        candidates.extend(parse_highlights(provider.complete_video(video_path, video_prompt), transcript.duration))
    if provider and not candidates:
        chunks = chunk_transcript(transcript)
        for index, chunk in enumerate(chunks, 1):
            desired = max(count + 2, math.ceil(count / len(chunks)) + 2)
            prompt = (
                f"Select up to {desired} excerpts lasting {min_duration}-{max_duration} seconds. "
                "Start at a natural hook and end after the thought resolves.\n\nTRANSCRIPT:\n"
                + transcript_text(chunk)
            )
            log(f"Analyzing transcript chunk {index}/{len(chunks)} with {provider.kind}")
            try:
                candidates.extend(parse_highlights(provider.complete(SYSTEM_PROMPT, prompt), transcript.duration))
            except ProviderError:
                raise
            except Exception as error:
                log(f"Could not parse provider result for chunk {index}: {error}")
    if not candidates:
        log("Using local transcript scoring (configure an API key for semantic AI ranking)")
        candidates = local_highlights(transcript, max(count * 3, count), min_duration, max_duration)
    valid = [item for item in candidates if min_duration * 0.6 <= item.end - item.start <= max_duration * 1.25]
    valid = dedupe(valid or candidates)
    return valid[:count]


def parse_highlights(raw: str, duration: float) -> list[Highlight]:
    cleaned = raw.strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE)
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if not match:
            raise ValueError("Provider did not return JSON")
        value = json.loads(match.group(0))
    items = value.get("highlights", value if isinstance(value, list) else [])
    results: list[Highlight] = []
    for item in items:
        try:
            highlight = Highlight.from_dict(item)
        except (TypeError, ValueError, KeyError):
            continue
        highlight.start = max(0, min(duration, highlight.start))
        highlight.end = max(highlight.start, min(duration, highlight.end))
        if highlight.end - highlight.start >= 5:
            results.append(highlight)
    return results


def local_highlights(transcript: Transcript, count: int, min_duration: int,
                     max_duration: int) -> list[Highlight]:
    """Useful no-key fallback based on speech density and hook vocabulary."""
    signals = {
        "why", "how", "secret", "mistake", "never", "best", "worst", "important",
        "surprising", "imagine", "actually", "problem", "solution", "learned", "truth",
        "cara", "kenapa", "rahasia", "kesalahan", "jangan", "penting", "ternyata",
    }
    candidates: list[Highlight] = []
    segments = transcript.segments
    for anchor, segment in enumerate(segments):
        start = segment.start
        selected: list[Segment] = []
        for item in segments[anchor:]:
            if item.end - start > max_duration:
                break
            selected.append(item)
            if item.end - start >= min_duration and _looks_complete(item.text):
                break
        if not selected or selected[-1].end - start < min_duration * 0.75:
            continue
        text = " ".join(item.text for item in selected).strip()
        words = re.findall(r"\b[\w'-]+\b", text.lower())
        density = len(words) / max(1, selected[-1].end - start)
        hook_hits = sum(word in signals for word in words[:35])
        punctuation = text.count("?") * 2 + text.count("!")
        score = min(88, round(48 + density * 4 + hook_hits * 6 + punctuation * 2))
        first_sentence = re.split(r"(?<=[.!?])\s+", text)[0][:110]
        title_words = re.findall(r"\b[\w'-]+\b", text)[:8]
        candidates.append(Highlight(
            start, selected[-1].end, " ".join(title_words).rstrip(".,!?"), first_sentence,
            text[:240], score, "Strong speech density and a self-contained thought",
        ))
    return dedupe(candidates)[:count]


def _looks_complete(text: str) -> bool:
    return text.rstrip().endswith((".", "!", "?"))


def dedupe(highlights: list[Highlight]) -> list[Highlight]:
    kept: list[Highlight] = []
    for candidate in sorted(highlights, key=lambda item: item.score, reverse=True):
        duration = max(0.1, candidate.end - candidate.start)
        overlaps = False
        for previous in kept:
            overlap = max(0.0, min(candidate.end, previous.end) - max(candidate.start, previous.start))
            if overlap / min(duration, previous.end - previous.start) > 0.5:
                overlaps = True
                break
        if not overlaps:
            kept.append(candidate)
    return kept

