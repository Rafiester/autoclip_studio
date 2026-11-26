from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Callable

import cv2

from .paths import CACHE_DIR
from .process import ProcessManager
from .smart_reframe import CropKeyframe, analyze_smart_reframe, keyframes_to_crop_x_expression


def _tracking_cache_path(source: Path, start: float, end: float, crop_width: int, sample_interval: float) -> Path | None:
    try:
        stat = source.stat()
    except OSError:
        return None
    key = f"v2:{source.resolve()}:{stat.st_size}:{stat.st_mtime_ns}:{start:.3f}:{end:.3f}:{crop_width}:{sample_interval:.3f}"
    digest = hashlib.sha256(key.encode()).hexdigest()[:32]
    return CACHE_DIR / "tracking" / f"{digest}.json"


def track_faces(source: Path, start: float, end: float, crop_width: int,
                manager: ProcessManager, log: Callable[[str], None],
                sample_interval: float = 0.75,
                cache_enabled: bool = False) -> list[tuple[float, float]]:
    """Backward-compatible face tracking entry point backed by Smart Reframing."""
    capture = cv2.VideoCapture(str(source))
    try:
        if not capture.isOpened():
            log("Face tracking unavailable; using centered crop")
            return []
        frame_width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        frame_height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    finally:
        capture.release()
    plan = analyze_smart_reframe(
        source, start, end, frame_width, frame_height, crop_width, (1080, 1920),
        manager, log, mode="single_subject", sample_interval=sample_interval, cache_enabled=cache_enabled,
        cache_dir=CACHE_DIR,
    )
    if not plan.segments or not plan.segments[0].keyframes:
        return []
    return [(kf.time, kf.left) for kf in plan.segments[0].keyframes]


def crop_x_expression(points: list[tuple[float, float]] | list[CropKeyframe], fallback: float) -> str:
    if not points:
        return str(round(fallback, 2))
    if isinstance(points[0], CropKeyframe):
        return keyframes_to_crop_x_expression(points, fallback)  # type: ignore[arg-type]
    converted = [CropKeyframe(float(p[0]), float(p[1]), False) for p in points]  # type: ignore[index]
    return keyframes_to_crop_x_expression(converted, fallback)
