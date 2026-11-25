"""Local automatic clip thumbnail extraction, scoring, caching, and 1080x1920 export."""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QLinearGradient, QPainter, QPainterPath

from .captions import TEMPLATES, TEMPLATE_DEFAULT, template_font


@dataclass(frozen=True, slots=True)
class ThumbnailResult:
    preview_path: Path
    raw_path: Path
    timestamp: float
    cache_key: str
    score: float


def thumbnail_cache_key(clip_path: Path, *, title: str = "", timestamp: float | None = None) -> str:
    try:
        stat = clip_path.stat()
        file_sig = f"{stat.st_size}:{stat.st_mtime_ns}"
    except OSError:
        file_sig = "missing"
    payload = {
        "schema": "thumb-v1",
        "path": str(clip_path.resolve()),
        "file": file_sig,
        "title": title,
        "timestamp": round(timestamp, 3) if timestamp is not None else "auto",
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:32]


def _sample_count(profile: str) -> int:
    return {"eco": 6, "balanced": 10, "turbo": 14}.get(profile, 10)


def max_thumbnail_workers(profile: str) -> int:
    return {"eco": 1, "balanced": 2, "turbo": 3}.get(profile, 2)


def _bgr_to_qimage(frame_bgr: np.ndarray) -> QImage:
    rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
    h, w, ch = rgb.shape
    return QImage(rgb.data, w, h, ch * w, QImage.Format.Format_RGB888).copy()


def _qimage_to_bgr(image: QImage) -> np.ndarray:
    rgb = image.convertToFormat(QImage.Format.Format_RGB888)
    w, h = rgb.width(), rgb.height()
    ptr = rgb.constBits()
    arr = np.frombuffer(ptr, dtype=np.uint8).reshape((h, rgb.bytesPerLine()))
    return cv2.cvtColor(arr[:, : w * 3].reshape((h, w, 3)), cv2.COLOR_RGB2BGR)


def score_frame(
    frame_bgr: np.ndarray,
    prev_gray: np.ndarray | None = None,
    cascade: cv2.CascadeClassifier | None = None,
) -> tuple[float, np.ndarray, bool]:
    """Score a candidate frame locally for sharpness, exposure, contrast, motion, and faces."""
    gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
    mean = float(np.mean(gray))
    std = float(np.std(gray))
    # Reject near-black, blown-out, or flat frames.
    if mean < 18.0 or mean > 245.0 or std < 8.0:
        return -1.0, gray, False

    lap_var = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    sharpness_score = min(100.0, math.log1p(max(0.0, lap_var)) * 14.0)
    exposure_score = max(0.0, 40.0 - abs(mean - 115.0) * 0.35)
    contrast_score = min(30.0, std * 0.55)

    motion_penalty = 0.0
    if prev_gray is not None and prev_gray.shape == gray.shape:
        diff = float(np.mean(cv2.absdiff(gray, prev_gray)))
        if diff > 42.0:
            motion_penalty = min(35.0, (diff - 42.0) * 0.8)

    face_bonus = 0.0
    if cascade is not None and not cascade.empty():
        scale = min(1.0, 480.0 / max(1, gray.shape[1]))
        small = cv2.resize(gray, None, fx=scale, fy=scale) if scale < 1.0 else gray
        faces = cascade.detectMultiScale(small, 1.12, 4, minSize=(24, 24))
        if len(faces):
            frame_area = max(1, small.shape[0] * small.shape[1])
            total_face_area = sum(int(w) * int(h) for (_x, _y, w, h) in faces)
            coverage = min(0.35, total_face_area / frame_area)
            face_bonus = 45.0 + coverage * 120.0 + min(3, len(faces)) * 6.0

    total = sharpness_score + exposure_score + contrast_score + face_bonus - motion_penalty
    return total, gray, True


def extract_frame_at(clip_path: Path, timestamp: float) -> QImage:
    """Extract a single frame at `timestamp` seconds from `clip_path` and release capture."""
    capture = cv2.VideoCapture(str(clip_path))
    try:
        if not capture.isOpened():
            raise RuntimeError(f"Could not open video for frame extraction: {clip_path}")
        fps = capture.get(cv2.CAP_PROP_FPS) or 30.0
        frame_count = capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0
        duration = frame_count / fps if fps > 0 and frame_count > 0 else 0.0
        clamped = max(0.0, min(max(0.0, duration - 0.05), timestamp)) if duration > 0 else max(0.0, timestamp)
        capture.set(cv2.CAP_PROP_POS_MSEC, clamped * 1000.0)
        ok, frame = capture.read()
        if not ok or frame is None:
            capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = capture.read()
        if not ok or frame is None:
            raise RuntimeError(f"Could not decode frame at {timestamp:.2f}s from {clip_path}")
        return _bgr_to_qimage(frame)
    finally:
        capture.release()


def _save_preview_and_raw(frame_bgr: np.ndarray, raw_path: Path, preview_path: Path, preview_width: int = 360) -> None:
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    ok_raw, buf_raw = cv2.imencode(".jpg", frame_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
    if not ok_raw:
        raise RuntimeError("Failed to encode raw thumbnail image")
    tmp_raw = raw_path.with_suffix(".raw.tmp.jpg")
    tmp_raw.write_bytes(buf_raw.tobytes())
    tmp_raw.replace(raw_path)

    h, w = frame_bgr.shape[:2]
    if w > preview_width:
        scale = preview_width / float(w)
        preview_bgr = cv2.resize(
            frame_bgr,
            (preview_width, max(1, int(round(h * scale)))),
            interpolation=cv2.INTER_AREA,
        )
    else:
        preview_bgr = frame_bgr
    ok_prev, buf_prev = cv2.imencode(".jpg", preview_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 86])
    if not ok_prev:
        raise RuntimeError("Failed to encode preview thumbnail image")
    tmp_prev = preview_path.with_suffix(".thumb.tmp.jpg")
    tmp_prev.write_bytes(buf_prev.tobytes())
    tmp_prev.replace(preview_path)


def generate_clip_thumbnail(
    clip_path: Path,
    output_dir: Path | None = None,
    *,
    title: str = "",
    timestamp: float | None = None,
    profile: str = "balanced",
    force: bool = False,
    existing_cache_key: str = "",
) -> ThumbnailResult:
    """Select the best frame from `clip_path` (or extract `timestamp`) and save raw + preview images."""
    thumb_dir = output_dir or (clip_path.parent / "thumbnails")
    raw_path = thumb_dir / f"{clip_path.stem}.raw.jpg"
    preview_path = thumb_dir / f"{clip_path.stem}.thumb.jpg"
    cache_key = thumbnail_cache_key(clip_path, title=title, timestamp=timestamp)

    if (
        not force
        and existing_cache_key == cache_key
        and raw_path.is_file()
        and preview_path.is_file()
    ):
        return ThumbnailResult(preview_path, raw_path, timestamp or 0.0, cache_key, 0.0)

    if timestamp is not None:
        image = extract_frame_at(clip_path, timestamp)
        frame_bgr = _qimage_to_bgr(image)
        _save_preview_and_raw(frame_bgr, raw_path, preview_path)
        return ThumbnailResult(preview_path, raw_path, timestamp, cache_key, 100.0)

    capture = cv2.VideoCapture(str(clip_path))
    try:
        if not capture.isOpened():
            raise RuntimeError(f"Could not open clip for thumbnail generation: {clip_path}")
        fps = capture.get(cv2.CAP_PROP_FPS) or 30.0
        frame_count = capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0
        duration = frame_count / fps if fps > 0 and frame_count > 0 else 1.0
        n_samples = _sample_count(profile)
        start_t = duration * 0.06
        end_t = max(start_t + 0.1, duration * 0.94)
        step = (end_t - start_t) / max(1, n_samples - 1)
        candidates_t = [min(duration - 0.02, start_t + i * step) for i in range(n_samples)]

        cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        prev_gray: np.ndarray | None = None
        best_frame: np.ndarray | None = None
        best_t = duration * 0.5
        best_score = -1e9
        midpoint_frame: np.ndarray | None = None

        for t in candidates_t:
            capture.set(cv2.CAP_PROP_POS_MSEC, max(0.0, t) * 1000.0)
            ok, frame = capture.read()
            if not ok or frame is None:
                continue
            score, gray, usable = score_frame(frame, prev_gray=prev_gray, cascade=cascade)
            prev_gray = gray
            if usable and score > best_score:
                best_score = score
                best_frame = frame.copy()
                best_t = t

        if best_frame is None:
            # Fall back to midpoint frame
            mid_t = max(0.0, duration * 0.5)
            capture.set(cv2.CAP_PROP_POS_MSEC, mid_t * 1000.0)
            ok, frame = capture.read()
            if not ok or frame is None:
                capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
                ok, frame = capture.read()
            if not ok or frame is None:
                raise RuntimeError(f"No readable frames in {clip_path}")
            midpoint_frame = frame.copy()
            best_frame = midpoint_frame
            best_t = mid_t
            best_score = 0.0
    finally:
        capture.release()

    _save_preview_and_raw(best_frame, raw_path, preview_path)
    return ThumbnailResult(preview_path, raw_path, best_t, cache_key, best_score)


def export_thumbnail(
    source_path: Path,
    target_path: Path,
    *,
    title: str = "",
    overlay_title: bool = False,
    size: tuple[int, int] = (1080, 1920),
    template_name: str = TEMPLATE_DEFAULT,
    timestamp: float | None = None,
) -> Path:
    """Export a high-resolution thumbnail (default 1080x1920) with optional branded title overlay."""
    if source_path.suffix.lower() in {".mp4", ".mkv", ".mov", ".webm", ".avi"}:
        image = extract_frame_at(source_path, timestamp or 0.0)
    else:
        image = QImage(str(source_path))
    if image.isNull():
        raise RuntimeError(f"Could not load thumbnail source: {source_path}")

    out_w, out_h = size
    canvas = QImage(out_w, out_h, QImage.Format.Format_ARGB32_Premultiplied)
    canvas.fill(QColor("#101116"))
    painter = QPainter(canvas)
    painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)

    scaled = image.scaled(out_w, out_h, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)
    sx = max(0, (scaled.width() - out_w) / 2.0)
    sy = max(0, (scaled.height() - out_h) / 2.0)
    painter.drawImage(QRectF(0, 0, out_w, out_h), scaled, QRectF(sx, sy, out_w, out_h))

    if overlay_title and title.strip():
        scrim_top = out_h * 0.62
        scrim = QLinearGradient(0, scrim_top, 0, out_h)
        scrim.setColorAt(0.0, QColor(16, 17, 22, 0))
        scrim.setColorAt(0.5, QColor(16, 17, 22, 185))
        scrim.setColorAt(1.0, QColor(16, 17, 22, 238))
        painter.fillRect(QRectF(0, scrim_top, out_w, out_h - scrim_top), scrim)

        # Red accent bar following brand guidelines
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#E53935"))
        painter.drawRoundedRect(QRectF(64, out_h - 320, 96, 8), 4, 4)

        template = TEMPLATES.get(template_name, TEMPLATES[TEMPLATE_DEFAULT])
        font = QFont(template_font(template))
        font.setBold(True)
        font.setPixelSize(max(36, int(out_w * 0.062)))
        painter.setFont(font)
        text_rect = QRectF(64, out_h - 295, out_w - 128, 220)
        # Subtle shadow for legibility
        painter.setPen(QColor(16, 17, 22, 210))
        painter.drawText(text_rect.translated(0, 3), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap, title.strip())
        painter.setPen(QColor("#F5F5F7"))
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap, title.strip())

    painter.end()
    target_path.parent.mkdir(parents=True, exist_ok=True)
    fmt = "PNG" if target_path.suffix.lower() == ".png" else "JPG"
    if not canvas.save(str(target_path), fmt, 95):
        raise RuntimeError(f"Could not save exported thumbnail to {target_path}")
    return target_path
