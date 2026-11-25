from __future__ import annotations

from pathlib import Path
from typing import Callable

import cv2

from .process import ProcessManager


def track_faces(source: Path, start: float, end: float, crop_width: int,
                manager: ProcessManager, log: Callable[[str], None],
                sample_interval: float = 0.75) -> list[tuple[float, float]]:
    capture = cv2.VideoCapture(str(source))
    try:
        if not capture.isOpened():
            log("Face tracking unavailable; using centered crop")
            return []
        frame_width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        points: list[tuple[float, float]] = []
        smoothed = frame_width / 2
        current = start
        while current <= end:
            manager.check_cancelled()
            capture.set(cv2.CAP_PROP_POS_MSEC, current * 1000)
            ok, frame = capture.read()
            if not ok:
                break
            scale = min(1.0, 640 / max(1, frame.shape[1]))
            preview = cv2.resize(frame, None, fx=scale, fy=scale) if scale < 1 else frame
            gray = cv2.cvtColor(preview, cv2.COLOR_BGR2GRAY)
            faces = cascade.detectMultiScale(gray, 1.12, 5, minSize=(35, 35))
            target = frame_width / 2
            if len(faces):
                x, _y, width, _height = max(faces, key=lambda face: face[2] * face[3])
                target = (x + width / 2) / scale
            smoothed = 0.25 * target + 0.75 * smoothed
            left = max(0, min(frame_width - crop_width, smoothed - crop_width / 2))
            points.append((current - start, left))
            current += sample_interval
    finally:
        capture.release()
    log(f"Tracked faces at {len(points)} sample points")
    return points


def crop_x_expression(points: list[tuple[float, float]], fallback: float) -> str:
    if not points:
        return str(round(fallback, 2))
    def piecewise(first: int, last: int) -> str:
        if first == last:
            time_a, x_a = points[first]
            if first == len(points) - 1:
                return str(round(x_a, 2))
            time_b, x_b = points[first + 1]
            span = max(0.01, time_b - time_a)
            return f"{x_a:.2f}+({x_b - x_a:.2f})*(t-{time_a:.2f})/{span:.2f}"
        middle = (first + last + 1) // 2
        # A balanced expression stays within FFmpeg's evaluator depth for long clips.
        time = points[middle][0]
        earlier = piecewise(first, middle - 1)
        later = piecewise(middle, last)
        return f"if(lt(t,{time:.2f}),{earlier},{later})"

    return piecewise(0, len(points) - 1)
