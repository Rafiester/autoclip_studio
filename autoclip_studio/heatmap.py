"""YouTube audience replay heatmap parsing, peak detection, region merging, and scoring."""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class HeatmapPoint:
    start_time: float
    end_time: float
    value: float  # Normalized relative replay intensity in [0.0, 1.0]

    @property
    def midpoint(self) -> float:
        return (self.start_time + self.end_time) * 0.5

    def to_dict(self) -> dict[str, float]:
        return {
            "start_time": round(self.start_time, 3),
            "end_time": round(self.end_time, 3),
            "value": round(self.value, 4),
        }


@dataclass(frozen=True, slots=True)
class HighlightRegion:
    start: float
    end: float
    peak_time: float
    peak_intensity: float
    mean_intensity: float
    replay_score: int  # Deterministic 0-100 relative popularity score


@dataclass(frozen=True, slots=True)
class HeatmapSummary:
    points: tuple[HeatmapPoint, ...]
    regions: tuple[HighlightRegion, ...]
    peak_timestamps: tuple[float, ...]
    available: bool


def format_clock(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def parse_heatmap(raw_heatmap: object, duration: float | None = None) -> list[HeatmapPoint]:
    """Parse and normalize yt-dlp heatmap entries without ever fabricating replay data."""
    if not isinstance(raw_heatmap, (list, tuple)) or not raw_heatmap:
        return []
    parsed: list[tuple[float, float, float]] = []
    max_val = 0.0
    for item in raw_heatmap:
        if not isinstance(item, dict):
            continue
        try:
            start = float(item["start_time"])
            end = float(item["end_time"])
            value = float(item["value"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (math.isfinite(start) and math.isfinite(end) and math.isfinite(value)):
            continue
        start = max(0.0, start)
        if duration is not None and duration > 0:
            end = min(float(duration), end)
        if end <= start:
            continue
        value = max(0.0, value)
        max_val = max(max_val, value)
        parsed.append((start, end, value))
    if not parsed or max_val <= 0.0:
        return []
    scale = max_val if max_val > 1.0 else 1.0
    points = [
        HeatmapPoint(start, end, min(1.0, max(0.0, val / scale)))
        for start, end, val in parsed
    ]
    points.sort(key=lambda pt: (pt.start_time, pt.end_time))
    return points


def region_replay_score(start: float, end: float, points: list[HeatmapPoint] | tuple[HeatmapPoint, ...]) -> int:
    """Deterministically score a clip interval [start, end] (0-100) from overlapping heatmap points."""
    if not points or end <= start:
        return 0
    weighted_sum = 0.0
    covered = 0.0
    peak = 0.0
    for pt in points:
        if pt.end_time <= start:
            continue
        if pt.start_time >= end:
            break
        overlap = min(end, pt.end_time) - max(start, pt.start_time)
        if overlap > 0:
            weighted_sum += overlap * pt.value
            covered += overlap
            if pt.value > peak:
                peak = pt.value
    if covered <= 0.0:
        return 0
    mean_val = weighted_sum / covered
    # Blend peak intensity (65%) and sustained mean intensity (35%) for balanced replay ranking.
    combined = 0.65 * peak + 0.35 * mean_val
    return max(0, min(100, int(round(combined * 100.0))))


def detect_highlight_regions(
    points: list[HeatmapPoint] | tuple[HeatmapPoint, ...],
    *,
    min_duration: float = 25.0,
    max_duration: float = 60.0,
    video_duration: float | None = None,
    max_regions: int = 8,
) -> list[HighlightRegion]:
    """Detect replay peaks, expand to neighboring high-intensity segments, and merge overlapping regions."""
    if not points:
        return []
    total_duration = float(video_duration) if (video_duration and video_duration > 0) else points[-1].end_time
    values = [pt.value for pt in points]
    mean_v = sum(values) / len(values)
    variance = sum((v - mean_v) ** 2 for v in values) / len(values)
    std_v = math.sqrt(variance)

    # Adaptive high-intensity threshold; falls back to top 20% if distribution is tight.
    sorted_vals = sorted(values)
    p80 = sorted_vals[min(len(sorted_vals) - 1, int(len(sorted_vals) * 0.80))]
    threshold = min(0.85, max(0.50, mean_v + 0.75 * std_v, p80 * 0.92))
    neighbor_floor = max(0.30, threshold * 0.72)

    # 1. Identify local peaks above threshold (or global max if above 0.4).
    peak_indices: list[int] = []
    for i, pt in enumerate(points):
        left = points[i - 1].value if i > 0 else -1.0
        right = points[i + 1].value if i + 1 < len(points) else -1.0
        if pt.value >= threshold and pt.value >= left and pt.value >= right:
            peak_indices.append(i)
    if not peak_indices:
        best_i = max(range(len(points)), key=lambda idx: points[idx].value)
        if points[best_i].value >= 0.35:
            peak_indices.append(best_i)
    if not peak_indices:
        return []

    # 2. Expand each peak to neighboring high-intensity segments.
    raw_windows: list[tuple[float, float, float, float]] = []
    target_span = min(max_duration, max(min_duration, (min_duration + max_duration) * 0.5))
    for idx in peak_indices:
        lo = idx
        hi = idx
        while lo > 0 and points[lo - 1].value >= neighbor_floor:
            if points[hi].end_time - points[lo - 1].start_time > max_duration:
                break
            lo -= 1
        while hi + 1 < len(points) and points[hi + 1].value >= neighbor_floor:
            if points[hi + 1].end_time - points[lo].start_time > max_duration:
                break
            hi += 1
        start = points[lo].start_time
        end = points[hi].end_time
        peak_pt = points[idx]
        # Pad short peaks gently around the peak midpoint so candidates satisfy min_duration.
        if end - start < min_duration and total_duration > 0:
            center = peak_pt.midpoint
            half = min(target_span, total_duration) * 0.5
            start = max(0.0, center - half)
            end = min(total_duration, start + min(target_span, total_duration))
            start = max(0.0, end - min(target_span, total_duration))
        raw_windows.append((start, end, peak_pt.midpoint, peak_pt.value))

    # 3. Merge adjacent or overlapping highlight regions.
    raw_windows.sort(key=lambda item: item[0])
    merge_gap = min(10.0, max(3.0, min_duration * 0.25))
    merged: list[list[float]] = []
    for start, end, peak_time, peak_val in raw_windows:
        if merged and start <= merged[-1][1] + merge_gap:
            prev = merged[-1]
            combined_end = max(prev[1], end)
            if combined_end - prev[0] <= max_duration * 1.15:
                prev[1] = combined_end
                if peak_val > prev[3]:
                    prev[2] = peak_time
                    prev[3] = peak_val
                continue
            # Keep stronger peak if merging would exceed max_duration.
            if peak_val > prev[3]:
                prev[0], prev[1], prev[2], prev[3] = start, end, peak_time, peak_val
            continue
        merged.append([start, end, peak_time, peak_val])

    # 4. Build scored HighlightRegion objects, ranked by replay_score desc.
    regions: list[HighlightRegion] = []
    for start, end, peak_time, peak_val in merged:
        clamped_end = min(total_duration, end) if total_duration > 0 else end
        if clamped_end <= start:
            continue
        r_score = region_replay_score(start, clamped_end, points)
        overlapping = [pt.value for pt in points if pt.end_time > start and pt.start_time < clamped_end]
        mean_intensity = sum(overlapping) / len(overlapping) if overlapping else peak_val
        regions.append(HighlightRegion(
            start=round(start, 2),
            end=round(clamped_end, 2),
            peak_time=round(peak_time, 2),
            peak_intensity=round(peak_val, 4),
            mean_intensity=round(mean_intensity, 4),
            replay_score=r_score,
        ))
    regions.sort(key=lambda r: (r.replay_score, r.peak_intensity), reverse=True)
    return regions[:max_regions]


def summarize_heatmap(
    points: list[HeatmapPoint] | tuple[HeatmapPoint, ...],
    *,
    min_duration: float = 25.0,
    max_duration: float = 60.0,
    video_duration: float | None = None,
) -> HeatmapSummary:
    if not points:
        return HeatmapSummary(points=(), regions=(), peak_timestamps=(), available=False)
    regions = detect_highlight_regions(
        points, min_duration=min_duration, max_duration=max_duration, video_duration=video_duration,
    )
    peaks = tuple(r.peak_time for r in regions[:5])
    return HeatmapSummary(
        points=tuple(points),
        regions=tuple(regions),
        peak_timestamps=peaks,
        available=True,
    )


def format_heatmap_prompt_summary(regions: list[HighlightRegion] | tuple[HighlightRegion, ...]) -> str:
    """Compact summary of audience replay peaks for AI prompt injection."""
    if not regions:
        return ""
    lines = ["YOUTUBE AUDIENCE REPLAY PEAKS (relative popularity signal, not guaranteed virality):"]
    for idx, region in enumerate(regions, 1):
        lines.append(
            f"  {idx}. [{region.start:.1f}s - {region.end:.1f}s] "
            f"peak at {region.peak_time:.1f}s ({format_clock(region.peak_time)}), "
            f"relative replay score {region.replay_score}/100"
        )
    return "\n".join(lines)
