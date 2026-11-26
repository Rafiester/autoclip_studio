from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from .heatmap import (
    HeatmapPoint, HighlightRegion, detect_highlight_regions,
    format_heatmap_prompt_summary, region_replay_score,
)
from .models import Highlight, Segment, Transcript
from .paths import CACHE_DIR
from .process import CancelledError, ProcessManager
from .providers import AIProvider, ProviderError


HIGHLIGHT_MODES = ("hybrid", "ai", "most_replayed")

SYSTEM_PROMPT = """You are an expert short-form video editor. Select contextually complete,
high-retention excerpts from a timestamped transcript. Favor immediate hooks, useful insights,
emotion, surprising claims, story turns, and quotable conclusions. Never invent timestamps or
content. Return only a JSON object with a `highlights` array. Each item must contain start, end,
title, hook, description, score (0-100), and reason."""

HYBRID_SYSTEM_PROMPT = """You are an expert short-form video editor performing Hybrid highlight detection.
You are given a timestamped transcript AND optional YouTube audience replay peak regions (relative popularity signals, not exact counts).
For every candidate clip, evaluate:
- Opening hook strength, content relevance, standalone context, emotional impact, informational value, and short-form suitability.
- Natural beginning and ending boundaries: inspect transcript context surrounding replay peaks and adjust start/end timestamps so sentences or conversations are never cut awkwardly.
- Coverage: evaluate replay peaks carefully, AND also select strong moments outside replay regions when their content quality is high.
Never invent timestamps or content. Return only a JSON object with a `highlights` array.
Each item must contain start, end, title, hook, description, score (0-100 AI content quality score), and reason (explaining why the clip was selected)."""


def combine_scores(
    ai_score: int,
    replay_score: int | None,
    *,
    mode: str = "hybrid",
    ai_weight: float = 0.6,
) -> int:
    """Deterministically calculate the final clip ranking score (0-100) in the backend."""
    ai_clamped = max(0, min(100, int(ai_score)))
    if replay_score is None or mode == "ai":
        return ai_clamped
    replay_clamped = max(0, min(100, int(replay_score)))
    if mode == "most_replayed":
        # Prioritize actual replay intensity while using content quality as a tie-breaker.
        return max(0, min(100, int(round(0.80 * replay_clamped + 0.20 * ai_clamped))))
    weight = max(0.0, min(1.0, float(ai_weight)))
    return max(0, min(100, int(round(weight * ai_clamped + (1.0 - weight) * replay_clamped))))


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
    mode: str = "ai", ai_weight: float = 0.6,
    heatmap_points: list[HeatmapPoint] | None = None,
) -> Path:
    transcript_digest = hashlib.sha256(
        json.dumps(transcript.to_dict(), sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    heatmap_digest = hashlib.sha256(
        json.dumps([pt.to_dict() for pt in (heatmap_points or [])], sort_keys=True).encode("utf-8")
    ).hexdigest() if heatmap_points else ""
    key = json.dumps({
        "schema": "v2",
        "transcript": transcript_digest,
        "count": count,
        "min_duration": min_duration,
        "max_duration": max_duration,
        "provider": provider.kind,
        "model": provider.model,
        "multimodal": multimodal,
        "mode": mode,
        "ai_weight": round(float(ai_weight), 3),
        "heatmap": heatmap_digest,
    }, sort_keys=True)
    return CACHE_DIR / "highlights" / f"{hashlib.sha256(key.encode('utf-8')).hexdigest()[:32]}.json"


def _align_region_to_transcript(
    region: HighlightRegion,
    transcript: Transcript,
    min_duration: int,
    max_duration: int,
) -> tuple[float, float, str]:
    """Snap a heatmap region to natural transcript sentence boundaries within duration bounds."""
    segments = transcript.segments
    if not segments:
        start = max(0.0, region.start)
        end = min(transcript.duration or region.end, max(start + min_duration, region.end))
        return start, min(end, start + max_duration), ""
    # Find segments overlapping the replay region.
    overlapping = [i for i, seg in enumerate(segments) if seg.end >= region.start and seg.start <= region.end]
    if not overlapping:
        # Closest segment to peak_time.
        nearest = min(range(len(segments)), key=lambda i: abs((segments[i].start + segments[i].end) * 0.5 - region.peak_time))
        lo = hi = nearest
    else:
        lo, hi = overlapping[0], overlapping[-1]

    # Expand backward slightly to start on a sentence boundary without exceeding max_duration.
    while lo > 0 and not _looks_complete(segments[lo - 1].text):
        if segments[hi].end - segments[lo - 1].start > max_duration:
            break
        lo -= 1
    # Expand forward to complete the thought and satisfy min_duration.
    while hi + 1 < len(segments):
        span = segments[hi].end - segments[lo].start
        if span >= min_duration and _looks_complete(segments[hi].text):
            break
        if segments[hi + 1].end - segments[lo].start > max_duration:
            break
        hi += 1
    # If still shorter than min_duration, expand backward if possible.
    while lo > 0 and (segments[hi].end - segments[lo].start) < min_duration:
        if segments[hi].end - segments[lo - 1].start > max_duration:
            break
        lo -= 1

    chosen = segments[lo:hi + 1]
    text = " ".join(seg.text for seg in chosen).strip()
    return chosen[0].start, chosen[-1].end, text


def most_replayed_highlights(
    transcript: Transcript,
    heatmap_points: list[HeatmapPoint],
    count: int,
    min_duration: int,
    max_duration: int,
    *,
    mode: str = "most_replayed",
    ai_weight: float = 0.6,
) -> list[Highlight]:
    """Build boundary-aligned highlight candidates anchored on YouTube Most Replayed regions."""
    regions = detect_highlight_regions(
        heatmap_points,
        min_duration=float(min_duration),
        max_duration=float(max_duration),
        video_duration=transcript.duration,
        max_regions=max(count * 3, 8),
    )
    candidates: list[Highlight] = []
    for region in regions:
        start, end, text = _align_region_to_transcript(region, transcript, min_duration, max_duration)
        if end - start < max(5.0, min_duration * 0.5):
            continue
        words = re.findall(r"\b[\w'-]+\b", text)
        density = len(words) / max(1.0, end - start)
        local_ai = min(88, max(45, round(52 + density * 4 + (4 if "?" in text or "!" in text else 0))))
        r_score = region_replay_score(start, end, heatmap_points)
        final_score = combine_scores(local_ai, r_score, mode=mode, ai_weight=ai_weight)
        first_sentence = re.split(r"(?<=[.!?])\s+", text)[0][:110] if text else "Most replayed moment"
        title = " ".join(words[:8]).rstrip(".,!?") or f"Most Replayed ({int(region.peak_time)}s)"
        reason = (
            f"Audience replay peak near {int(region.peak_time)}s "
            f"(relative replay score {r_score}/100) aligned to natural transcript boundaries."
        )
        candidates.append(Highlight(
            start=start,
            end=end,
            title=title,
            hook=first_sentence,
            description=text[:240],
            score=final_score,
            reason=reason,
            ai_score=local_ai,
            replay_score=r_score,
            detection_mode=mode,
        ))
    return dedupe(candidates, limit=count)


def select_highlights(transcript: Transcript, count: int, min_duration: int,
                      max_duration: int, provider: AIProvider | None,
                      log: Callable[[str], None], video_path: Path | None = None,
                      multimodal: bool = False, *, manager: ProcessManager | None = None,
                      cache_enabled: bool = False,
                      mode: str = "ai",
                      heatmap_points: list[HeatmapPoint] | None = None,
                      ai_weight: float = 0.6) -> list[Highlight]:
    effective_mode = mode if mode in HIGHLIGHT_MODES else "ai"
    points = list(heatmap_points or [])
    if effective_mode in {"hybrid", "most_replayed"} and not points:
        log("YouTube audience heatmap is unavailable for this video; falling back to AI Analysis.")
        effective_mode = "ai"

    cache_path = (
        _highlights_cache_path(
            transcript, count, min_duration, max_duration, provider, multimodal,
            mode=effective_mode, ai_weight=ai_weight, heatmap_points=points,
        )
        if (cache_enabled and provider is not None) else None
    )
    if cache_path is not None and cache_path.is_file():
        try:
            cached = [Highlight.from_dict(item) for item in json.loads(cache_path.read_text(encoding="utf-8"))]
            if cached:
                log(f"Using cached {effective_mode} highlight selection ({len(cached)} clips)")
                return cached
        except (OSError, ValueError, TypeError, KeyError):
            pass

    if effective_mode == "most_replayed":
        log(f"Prioritizing YouTube Most Replayed peaks ({len(points)} heatmap points)")
        candidates = most_replayed_highlights(
            transcript, points, max(count * 2, count), min_duration, max_duration,
            mode="most_replayed", ai_weight=ai_weight,
        )
        valid = [item for item in candidates if min_duration * 0.6 <= item.end - item.start <= max_duration * 1.25]
        selected = dedupe(valid or candidates, limit=count)
        return selected

    regions = (
        detect_highlight_regions(
            points,
            min_duration=float(min_duration),
            max_duration=float(max_duration),
            video_duration=transcript.duration,
            max_regions=max(count * 2, 6),
        )
        if points else []
    )
    heatmap_context = format_heatmap_prompt_summary(regions) if effective_mode == "hybrid" else ""

    candidates: list[Highlight] = []
    if multimodal and provider and provider.kind == "gemini" and video_path:
        log("Uploading source to Gemini for optional multimodal analysis")
        video_prompt = (
            f"Analyze this video and select up to {count + 2} contextually complete highlights "
            f"lasting {min_duration}-{max_duration} seconds. Use timestamps from the video. "
            "Return a JSON object with a highlights array containing start, end, title, hook, "
            "description, score, and reason."
        )
        if heatmap_context:
            video_prompt = f"{video_prompt}\n\n{heatmap_context}"
        if manager is None:
            raw = provider.complete_video(video_path, video_prompt)
        else:
            raw = provider.complete_video(video_path, video_prompt, cancel_event=manager.cancel_event)
        candidates.extend(parse_highlights(raw, transcript.duration))
    if provider and not candidates:
        chunks = chunk_transcript(transcript)
        sys_prompt = HYBRID_SYSTEM_PROMPT if effective_mode == "hybrid" else SYSTEM_PROMPT
        for index, chunk in enumerate(chunks, 1):
            if manager:
                manager.check_cancelled()
            desired = max(count + 2, math.ceil(count / len(chunks)) + 2)
            chunk_start, chunk_end = chunk[0].start, chunk[-1].end
            chunk_regions = [r for r in regions if r.end >= chunk_start and r.start <= chunk_end]
            chunk_heatmap_block = (
                f"\n\n{format_heatmap_prompt_summary(chunk_regions)}\n"
                if (effective_mode == "hybrid" and chunk_regions) else ""
            )
            prompt = (
                f"Select up to {desired} excerpts lasting {min_duration}-{max_duration} seconds. "
                "Start at a natural hook and end after the thought resolves."
                f"{chunk_heatmap_block}\n\nTRANSCRIPT:\n"
                + transcript_text(chunk)
            )
            log(f"Analyzing transcript chunk {index}/{len(chunks)} with {provider.kind} ({effective_mode})")
            try:
                candidates.extend(parse_highlights(provider.complete(sys_prompt, prompt), transcript.duration))
            except (CancelledError, ProviderError):
                raise
            except Exception as error:
                log(f"Could not parse provider result for chunk {index}: {error}")
    if manager:
        manager.check_cancelled()
    if not candidates:
        log("Using local transcript scoring (configure an API key for semantic AI ranking)")
        candidates = local_highlights(transcript, max(count * 3, count), min_duration, max_duration, manager=manager)

    # Deterministically score every candidate in the backend.
    for item in candidates:
        raw_ai = item.ai_score if item.ai_score is not None else item.score
        item.ai_score = max(0, min(100, int(raw_ai)))
        item.detection_mode = effective_mode
        if points:
            item.replay_score = region_replay_score(item.start, item.end, points)
            item.score = combine_scores(item.ai_score, item.replay_score, mode=effective_mode, ai_weight=ai_weight)
        else:
            item.replay_score = None
            item.score = item.ai_score

    # In Hybrid mode, also ensure top replay peak regions are represented as eligible candidates.
    if effective_mode == "hybrid" and points:
        peak_candidates = most_replayed_highlights(
            transcript, points, max(count, 3), min_duration, max_duration,
            mode="hybrid", ai_weight=ai_weight,
        )
        candidates.extend(peak_candidates)

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

