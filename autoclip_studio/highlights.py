from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from .models import Highlight, Segment, Transcript
from .paths import CACHE_DIR
from .process import CancelledError, ProcessManager
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


def _highlights_cache_path(
    transcript: Transcript, count: int, min_duration: int, max_duration: int,
    provider: AIProvider, multimodal: bool,
) -> Path:
    transcript_digest = hashlib.sha256(
        json.dumps(transcript.to_dict(), sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    key = json.dumps({
        "schema": "v1",
        "transcript": transcript_digest,
        "count": count,
        "min_duration": min_duration,
        "max_duration": max_duration,
        "provider": provider.kind,
        "model": provider.model,
        "multimodal": multimodal,
    }, sort_keys=True)
    return CACHE_DIR / "highlights" / f"{hashlib.sha256(key.encode('utf-8')).hexdigest()[:32]}.json"


def select_highlights(transcript: Transcript, count: int, min_duration: int,
                      max_duration: int, provider: AIProvider | None,
                      log: Callable[[str], None], video_path: Path | None = None,
                      multimodal: bool = False, *, manager: ProcessManager | None = None,
                      cache_enabled: bool = False) -> list[Highlight]:
    cache_path = (
        _highlights_cache_path(transcript, count, min_duration, max_duration, provider, multimodal)
        if (cache_enabled and provider is not None) else None
    )
    if cache_path is not None and cache_path.is_file():
        try:
            cached = [Highlight.from_dict(item) for item in json.loads(cache_path.read_text(encoding="utf-8"))]
            if cached:
                log(f"Using cached AI highlight selection ({len(cached)} clips)")
                return cached
        except (OSError, ValueError, TypeError, KeyError):
            pass

    candidates: list[Highlight] = []
    if multimodal and provider and provider.kind == "gemini" and video_path:
        log("Uploading source to Gemini for optional multimodal analysis")
        video_prompt = (
            f"Analyze this video and select up to {count + 2} contextually complete highlights "
            f"lasting {min_duration}-{max_duration} seconds. Use timestamps from the video. "
            "Return a JSON object with a highlights array containing start, end, title, hook, "
            "description, score, and reason."
        )
        if manager is None:
            raw = provider.complete_video(video_path, video_prompt)
        else:
            raw = provider.complete_video(video_path, video_prompt, cancel_event=manager.cancel_event)
        candidates.extend(parse_highlights(raw, transcript.duration))
    if provider and not candidates:
        chunks = chunk_transcript(transcript)
        for index, chunk in enumerate(chunks, 1):
            if manager:
                manager.check_cancelled()
            desired = max(count + 2, math.ceil(count / len(chunks)) + 2)
            prompt = (
                f"Select up to {desired} excerpts lasting {min_duration}-{max_duration} seconds. "
                "Start at a natural hook and end after the thought resolves.\n\nTRANSCRIPT:\n"
                + transcript_text(chunk)
            )
            log(f"Analyzing transcript chunk {index}/{len(chunks)} with {provider.kind}")
            try:
                candidates.extend(parse_highlights(provider.complete(SYSTEM_PROMPT, prompt), transcript.duration))
            except (CancelledError, ProviderError):
                raise
            except Exception as error:
                log(f"Could not parse provider result for chunk {index}: {error}")
    if manager:
        manager.check_cancelled()
    if not candidates:
        log("Using local transcript scoring (configure an API key for semantic AI ranking)")
        candidates = local_highlights(transcript, max(count * 3, count), min_duration, max_duration, manager=manager)
    valid = [item for item in candidates if min_duration * 0.6 <= item.end - item.start <= max_duration * 1.25]
    selected = dedupe(valid or candidates, limit=count)
    if cache_path is not None and provider is not None and selected:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps([asdict(item) for item in selected], ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
    return selected


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
    items = value.get("highlights", []) if isinstance(value, dict) else value
    if not isinstance(items, list):
        return []
    results: list[Highlight] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            highlight = Highlight.from_dict(item)
        except (TypeError, ValueError, KeyError, OverflowError):
            continue
        if not math.isfinite(highlight.start) or not math.isfinite(highlight.end):
            continue
        highlight.start = max(0, min(duration, highlight.start))
        highlight.end = max(highlight.start, min(duration, highlight.end))
        if highlight.end - highlight.start >= 5:
            results.append(highlight)
    return results


def local_highlights(transcript: Transcript, count: int, min_duration: int,
                     max_duration: int, *, manager: ProcessManager | None = None) -> list[Highlight]:
    """Useful no-key fallback based on speech density and hook vocabulary."""
    signals = {
        "why", "how", "secret", "mistake", "never", "best", "worst", "important",
        "surprising", "imagine", "actually", "problem", "solution", "learned", "truth",
        "cara", "kenapa", "rahasia", "kesalahan", "jangan", "penting", "ternyata",
    }
    candidates: list[Highlight] = []
    segments = transcript.segments
    for anchor, segment in enumerate(segments):
        if manager and anchor % 128 == 0:
            manager.check_cancelled()
        start = segment.start
        selected: list[Segment] = []
        for index in range(anchor, len(segments)):
            item = segments[index]
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
    return dedupe(candidates, limit=count)


def _looks_complete(text: str) -> bool:
    return text.rstrip().endswith((".", "!", "?"))


def dedupe(highlights: list[Highlight], *, limit: int | None = None) -> list[Highlight]:
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
            if limit is not None and limit > 0 and len(kept) == limit:
                break
    return kept[:limit]

