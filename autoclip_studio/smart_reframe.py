"""Multi-person Active Speaker Detection (ASD) and adaptive 9:16 Smart Reframing engine.

Designed cleanly from first principles with zero unlicensed third-party code:
- Supports optional MIT-licensed OpenCV YuNet (`FaceDetectorYN`, 5-point facial landmarks)
  with built-in zero-download multi-orientation OpenCV Haar fallback (frontal + left/right profile).
- Audio-visual active speaker scoring via synchronized PyAV speech-band RMS VAD +
  head-motion-compensated mouth ROI visual energy.
- Scene-cut detection, persistent face tracks, dropout tolerance, minimum hold duration,
  switching hysteresis, dead-zone stabilization, clean cuts between distant speakers,
  and single active-speaker portrait framing in Auto Smart Reframe (Podcast Split Screen available explicitly).
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

from .auto_director import (
    REFRAME_STYLES,
    VALID_REFRAME_STYLES,
    DirectorSample,
    TrackObservation,
    direct_single_stream,
    get_style_config,
)
from .models import Transcript
from .paths import CACHE_DIR, MODEL_CACHE
from .process import ProcessManager


REFRAME_MODES: tuple[tuple[str, str], ...] = (
    ("auto", "Auto Smart Reframe"),
    ("active_speaker", "Active Speaker"),
    ("split_screen", "Podcast Split Screen"),
    ("single_subject", "Single Subject Tracking"),
    ("fit_original", "Fit Original Video"),
    ("off", "Off (Center Crop)"),
)
VALID_REFRAME_MODES = frozenset(code for code, _ in REFRAME_MODES)

YUNET_FILENAME = "face_detection_yunet_2023mar.onnx"
YUNET_URL = (
    "https://github.com/opencv/opencv_zoo/raw/main/models/"
    "face_detection_yunet/face_detection_yunet_2023mar.onnx"
)


@dataclass(slots=True)
class FaceDetection:
    x: float
    y: float
    w: float
    h: float
    score: float = 1.0
    mouth_box: tuple[float, float, float, float] | None = None  # (mx, my, mw, mh) in full-frame coords

    @property
    def cx(self) -> float:
        return self.x + self.w * 0.5

    @property
    def cy(self) -> float:
        return self.y + self.h * 0.5

    @property
    def area(self) -> float:
        return max(0.0, self.w * self.h)


@dataclass(slots=True)
class CropKeyframe:
    time: float       # Relative to clip start (seconds)
    left: float       # Horizontal crop left offset in source pixels
    cut: bool = False  # True -> instant step cut instead of linear pan


@dataclass(slots=True)
class FramingSegment:
    kind: str         # "single" | "split" | "fit"
    start: float      # Relative to clip start (seconds)
    end: float        # Relative to clip start (seconds)
    keyframes: list[CropKeyframe] = field(default_factory=list)
    top_box: tuple[int, int, int, int] | None = None     # (x, y, w, h) for split top pane
    bottom_box: tuple[int, int, int, int] | None = None  # (x, y, w, h) for split bottom pane


@dataclass(slots=True)
class FramingMetrics:
    mode_used: str = "auto"
    style_used: str = "natural"
    detector_backend: str = "opencv-haar-multi"
    frames_analyzed: int = 0
    scene_cuts: int = 0
    speaker_switches: int = 0
    midpoint_avoided_frames: int = 0
    split_segments: int = 0
    single_segments: int = 0
    median_shot_duration: float = 0.0
    crop_jitter_px_s: float = 0.0
    face_visibility_ratio: float = 0.0


@dataclass(slots=True)
class FramingPlan:
    segments: list[FramingSegment]
    metrics: FramingMetrics = field(default_factory=FramingMetrics)
    has_split: bool = False

    def to_dict(self) -> dict:
        return {
            "has_split": self.has_split,
            "metrics": asdict(self.metrics),
            "segments": [
                {
                    "kind": s.kind,
                    "start": round(s.start, 3),
                    "end": round(s.end, 3),
                    "keyframes": [{"time": round(k.time, 3), "left": round(k.left, 2), "cut": k.cut} for k in s.keyframes],
                    "top_box": list(s.top_box) if s.top_box else None,
                    "bottom_box": list(s.bottom_box) if s.bottom_box else None,
                }
                for s in self.segments
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "FramingPlan":
        segments = []
        for item in data.get("segments", []):
            kfs = [CropKeyframe(float(k["time"]), float(k["left"]), bool(k.get("cut", False))) for k in item.get("keyframes", [])]
            top = tuple(int(v) for v in item["top_box"]) if item.get("top_box") else None
            bot = tuple(int(v) for v in item["bottom_box"]) if item.get("bottom_box") else None
            segments.append(FramingSegment(
                kind=str(item["kind"]),
                start=float(item["start"]),
                end=float(item["end"]),
                keyframes=kfs,
                top_box=top,  # type: ignore[arg-type]
                bottom_box=bot,  # type: ignore[arg-type]
            ))
        m_raw = data.get("metrics") or {}
        metrics = FramingMetrics(**{k: v for k, v in m_raw.items() if k in FramingMetrics.__dataclass_fields__})
        return cls(segments=segments, metrics=metrics, has_split=bool(data.get("has_split", False)))


def find_yunet_model() -> Path | None:
    """Return local YuNet ONNX model path if installed in cache/models or tools."""
    for candidate in (
        MODEL_CACHE / YUNET_FILENAME,
        CACHE_DIR / "models" / YUNET_FILENAME,
        Path(__file__).resolve().parent.parent / "tools" / "models" / YUNET_FILENAME,
    ):
        if candidate.is_file() and candidate.stat().st_size > 50_000:
            return candidate
    return None


def _iou(a: FaceDetection, b: FaceDetection) -> float:
    x1 = max(a.x, b.x)
    y1 = max(a.y, b.y)
    x2 = min(a.x + a.w, b.x + b.w)
    y2 = min(a.y + a.h, b.y + b.h)
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    if inter <= 0.0:
        return 0.0
    union = a.area + b.area - inter
    return inter / max(1e-6, union)


def _containment(a: FaceDetection, b: FaceDetection) -> float:
    x1 = max(a.x, b.x)
    y1 = max(a.y, b.y)
    x2 = min(a.x + a.w, b.x + b.w)
    y2 = min(a.y + a.h, b.y + b.h)
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    if inter <= 0.0:
        return 0.0
    return inter / max(1e-6, min(a.area, b.area))


def _nms_faces(dets: list[FaceDetection], iou_thresh: float = 0.38) -> list[FaceDetection]:
    ordered = sorted(dets, key=lambda d: (d.score, d.area), reverse=True)
    kept: list[FaceDetection] = []
    for det in ordered:
        if any(_iou(det, prev) > iou_thresh or _containment(det, prev) > 0.62 for prev in kept):
            continue
        kept.append(det)
    return kept


class _FaceDetector:
    """YuNet 5-point landmark detector when available, with multi-orientation Haar fallback."""

    def __init__(self) -> None:
        self._yunet = None
        self.backend = "opencv-haar-multi"
        yunet_path = find_yunet_model()
        if yunet_path is not None and hasattr(cv2, "FaceDetectorYN"):
            try:
                self._yunet = cv2.FaceDetectorYN.create(
                    str(yunet_path), "", (320, 320), score_threshold=0.58, nms_threshold=0.35, top_k=20,
                )
                self.backend = "opencv-yunet-landmarks"
            except Exception:
                self._yunet = None
        if self._yunet is None:
            base = cv2.data.haarcascades
            self._frontal_alt = cv2.CascadeClassifier(base + "haarcascade_frontalface_alt2.xml")
            self._frontal_def = cv2.CascadeClassifier(base + "haarcascade_frontalface_default.xml")
            self._profile = cv2.CascadeClassifier(base + "haarcascade_profileface.xml")

    def detect(self, frame_bgr: np.ndarray, scale: float) -> list[FaceDetection]:
        h, w = frame_bgr.shape[:2]
        inv = 1.0 / max(1e-6, scale)
        if self._yunet is not None:
            self._yunet.setInputSize((w, h))
            _, raw = self._yunet.detect(frame_bgr)
            if raw is None or len(raw) == 0:
                return []
            min_face = max(14.0, min(w, h) * 0.042) * inv
            results: list[FaceDetection] = []
            for row in raw:
                fx, fy, fw, fh = float(row[0]) * inv, float(row[1]) * inv, float(row[2]) * inv, float(row[3]) * inv
                if fw < min_face or fh < min_face:
                    continue
                # YuNet landmarks: right_eye(4,5), left_eye(6,7), nose(8,9), right_mouth(10,11), left_mouth(12,13)
                rmx, rmy = float(row[10]) * inv, float(row[11]) * inv
                lmx, lmy = float(row[12]) * inv, float(row[13]) * inv
                mcx, mcy = (rmx + lmx) * 0.5, (rmy + lmy) * 0.5
                mw = max(fw * 0.42, abs(lmx - rmx) * 1.45)
                mh = max(fh * 0.24, mw * 0.55)
                mouth_box = (mcx - mw * 0.5, mcy - mh * 0.45, mw, mh)
                results.append(FaceDetection(fx, fy, fw, fh, float(row[14]), mouth_box=mouth_box))
            return _nms_faces(results)

        gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
        min_side = max(18, int(min(w, h) * 0.05))
        dets: list[FaceDetection] = []
        for cascade, base_score in ((self._frontal_alt, 0.95), (self._frontal_def, 0.85)):
            if cascade.empty():
                continue
            found = cascade.detectMultiScale(gray, 1.12, 5, minSize=(min_side, min_side))
            for (x, y, fw, fh) in found:
                dets.append(FaceDetection(x * inv, y * inv, fw * inv, fh * inv, base_score))
        # Profile faces (left-facing & mirrored right-facing) for podcast hosts turning toward each other.
        if not self._profile.empty():
            for (x, y, fw, fh) in self._profile.detectMultiScale(gray, 1.12, 5, minSize=(min_side, min_side)):
                dets.append(FaceDetection(x * inv, y * inv, fw * inv, fh * inv, 0.78))
            flipped = cv2.flip(gray, 1)
            for (x, y, fw, fh) in self._profile.detectMultiScale(flipped, 1.12, 5, minSize=(min_side, min_side)):
                orig_x = w - (x + fw)
                dets.append(FaceDetection(orig_x * inv, y * inv, fw * inv, fh * inv, 0.78))
        return _filter_false_faces(_nms_faces(dets, iou_thresh=0.35), gray, scale, w * inv, h * inv)


def _filter_false_faces(
    dets: list[FaceDetection],
    gray_small: np.ndarray,
    scale: float,
    src_width: float,
    src_height: float,
) -> list[FaceDetection]:
    """Suppress common Haar false positives (dark backpacks/shirts, hands/chests below real faces, bottom-edge noise)."""
    if not dets:
        return []
    gh, gw = gray_small.shape[:2]
    # Step 1: Reject dark, low-texture fabric false positives (e.g. black chair/backpack/shirt folds)
    # when not backed by high-confidence frontal alt2 detection.
    plausible: list[FaceDetection] = []
    for d in dets:
        x0 = max(0, min(gw - 1, int(round(d.x * scale))))
        y0 = max(0, min(gh - 1, int(round(d.y * scale))))
        x1 = max(x0 + 1, min(gw, int(round((d.x + d.w) * scale))))
        y1 = max(y0 + 1, min(gh, int(round((d.y + d.h) * scale))))
        roi = gray_small[y0:y1, x0:x1]
        if roi.size > 0 and d.score < 0.90:
            mean_luma = float(roi.mean())
            std_luma = float(roi.std())
            if mean_luma < 36.0 and std_luma < 26.0:
                continue
        # Bottom 18% of frame is almost always table/chair/backpack, not a human head.
        if d.cy > src_height * 0.82 and d.score < 0.92:
            continue
        plausible.append(d)

    if len(plausible) <= 1:
        return plausible

    # Step 2: Reject detections sitting directly inside another larger/higher-scoring face's torso column
    # (prevents hands, lapels, or chest folds from spawning fake secondary speaker tracks!).
    ordered = sorted(plausible, key=lambda item: (item.score, item.area), reverse=True)
    kept: list[FaceDetection] = []
    for cand in ordered:
        is_torso_fp = False
        for upper in kept:
            horiz_aligned = abs(cand.cx - upper.cx) <= max(upper.w * 1.15, cand.w * 1.15)
            below_chin = cand.cy >= (upper.y + upper.h * 0.92)
            smaller_or_weaker = (cand.area <= upper.area * 0.92) or (cand.score < upper.score)
            if horiz_aligned and below_chin and smaller_or_weaker:
                is_torso_fp = True
                break
        if not is_torso_fp:
            kept.append(cand)
    return kept


def extract_audio_vad_envelope(
    source: Path,
    start: float,
    end: float,
    *,
    bin_seconds: float = 0.10,
    transcript: Transcript | None = None,
) -> tuple[np.ndarray, float]:
    """Extract normalized speech-band audio energy envelope [0..1] for [start, end] using PyAV."""
    duration = max(0.1, end - start)
    n_bins = max(1, int(math.ceil(duration / bin_seconds)))
    envelope = np.zeros(n_bins, dtype=np.float32)

    try:
        import av
        with av.open(str(source)) as container:
            audio_streams = [s for s in container.streams if s.type == "audio"]
            if audio_streams:
                stream = audio_streams[0]
                resampler = av.AudioResampler(format="fltp", layout="mono", rate=16000)
                if start > 0.2 and stream.time_base:
                    container.seek(int(max(0.0, start - 0.5) / float(stream.time_base)), stream=stream, any_frame=False)
                chunks: list[tuple[float, np.ndarray]] = []
                for frame in container.decode(stream):
                    pts_sec = float(frame.pts * stream.time_base) if (frame.pts is not None and stream.time_base) else None
                    if pts_sec is None:
                        continue
                    if pts_sec + (frame.samples / max(1, frame.sample_rate)) < start:
                        continue
                    if pts_sec > end + 0.25:
                        break
                    for resampled in resampler.resample(frame):
                        arr = resampled.to_ndarray()
                        mono = arr[0] if arr.ndim > 1 else arr
                        chunks.append((pts_sec, mono.astype(np.float32, copy=False)))
                if chunks:
                    sr = 16000
                    total_samples = int(math.ceil(duration * sr))
                    buf = np.zeros(total_samples, dtype=np.float32)
                    for pts_sec, mono in chunks:
                        offset = int(round((pts_sec - start) * sr))
                        src_lo = max(0, -offset)
                        dst_lo = max(0, offset)
                        length = min(len(mono) - src_lo, total_samples - dst_lo)
                        if length > 0:
                            buf[dst_lo:dst_lo + length] = mono[src_lo:src_lo + length]
                    step = max(1, int(round(bin_seconds * sr)))
                    rms_vals = np.zeros(n_bins, dtype=np.float32)
                    for i in range(n_bins):
                        seg = buf[i * step:min(total_samples, (i + 1) * step)]
                        if len(seg) >= 32:
                            # Speech-band energy weighting (300-3400 Hz) via lightweight real FFT.
                            spec = np.abs(np.fft.rfft(seg))
                            freqs = np.fft.rfftfreq(len(seg), d=1.0 / sr)
                            voice_mask = (freqs >= 250.0) & (freqs <= 3600.0)
                            voice_energy = float(np.sqrt(np.mean(spec[voice_mask] ** 2))) if np.any(voice_mask) else 0.0
                            rms_vals[i] = voice_energy
                    p90 = float(np.percentile(rms_vals, 90)) if np.any(rms_vals > 0) else 0.0
                    noise_floor = float(np.percentile(rms_vals, 15)) if np.any(rms_vals > 0) else 0.0
                    denom = max(1e-6, p90 - noise_floor)
                    if p90 > 1e-6:
                        envelope = np.clip((rms_vals - noise_floor) / denom, 0.0, 1.0)
    except Exception:
        pass

    # Optional supporting prior from transcript timestamps (never identifies WHICH face speaks).
    if transcript is not None and transcript.segments:
        has_audio_signal = bool(np.any(envelope > 0.05))
        for seg in transcript.segments:
            if seg.end < start or seg.start > end:
                continue
            b0 = max(0, int((seg.start - start) / bin_seconds))
            b1 = min(n_bins, int(math.ceil((seg.end - start) / bin_seconds)))
            if has_audio_signal:
                envelope[b0:b1] = np.clip(envelope[b0:b1] * 0.8 + 0.2, 0.0, 1.0)
            else:
                envelope[b0:b1] = 0.75
    return envelope, bin_seconds


def _extract_roi_patch(gray: np.ndarray, box: tuple[float, float, float, float], scale: float) -> np.ndarray | None:
    h, w = gray.shape[:2]
    x0 = max(0, min(w - 1, int(round(box[0] * scale))))
    y0 = max(0, min(h - 1, int(round(box[1] * scale))))
    x1 = max(x0 + 1, min(w, int(round((box[0] + box[2]) * scale))))
    y1 = max(y0 + 1, min(h, int(round((box[1] + box[3]) * scale))))
    if x1 - x0 < 4 or y1 - y0 < 4:
        return None
    patch = cv2.resize(gray[y0:y1, x0:x1], (28, 18), interpolation=cv2.INTER_AREA)
    mean = float(patch.mean())
    std = float(patch.std())
    if std < 1.0:
        return patch.astype(np.float32) - mean
    return (patch.astype(np.float32) - mean) / std


def _mouth_and_upper_boxes(det: FaceDetection) -> tuple[tuple[float, float, float, float], tuple[float, float, float, float]]:
    mouth = det.mouth_box or (det.x + det.w * 0.24, det.y + det.h * 0.60, det.w * 0.52, det.h * 0.28)
    upper = (det.x + det.w * 0.20, det.y + det.h * 0.18, det.w * 0.60, det.h * 0.30)
    return mouth, upper


def _is_scene_cut(prev_small: np.ndarray | None, curr_small: np.ndarray) -> bool:
    if prev_small is None or prev_small.shape != curr_small.shape:
        return False
    mad = float(np.mean(np.abs(curr_small.astype(np.float32) - prev_small.astype(np.float32))))
    if mad > 36.0:
        return True
    hist_a = cv2.calcHist([prev_small], [0], None, [32], [0, 256])
    hist_b = cv2.calcHist([curr_small], [0], None, [32], [0, 256])
    cv2.normalize(hist_a, hist_a)
    cv2.normalize(hist_b, hist_b)
    corr = float(cv2.compareHist(hist_a, hist_b, cv2.HISTCMP_CORREL))
    return corr < 0.62 and mad > 16.0


def _content_saliency_center_x(gray: np.ndarray, src_width: int) -> float:
    """Fallback horizontal focus for no-face B-roll frames based on edge/detail density."""
    edges = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    col_energy = np.mean(np.abs(edges), axis=0)
    total = float(col_energy.sum())
    if total <= 1e-4:
        return src_width * 0.5
    xs = np.linspace(0.0, float(src_width), len(col_energy), dtype=np.float32)
    return float(np.sum(xs * col_energy) / total)


def _clamp_crop_left(center_x: float, crop_width: int, src_width: int) -> float:
    return max(0.0, min(float(max(0, src_width - crop_width)), center_x - crop_width * 0.5))


def can_group_frame(faces: list[FaceDetection], crop_width: int, *, margin_ratio: float = 0.80) -> tuple[bool, float]:
    """Aspect-ratio feasibility check: return (fits_safely, group_center_x). Never returns midpoint for distant faces."""
    if not faces:
        return False, 0.0
    if len(faces) == 1:
        return True, faces[0].cx
    min_left = min(f.x for f in faces)
    max_right = max(f.x + f.w for f in faces)
    span = max_right - min_left
    if span <= crop_width * margin_ratio:
        return True, (min_left + max_right) * 0.5
    return False, 0.0


def compute_split_box(
    face: FaceDetection,
    src_width: int,
    src_height: int,
    pane_aspect: float,
) -> tuple[int, int, int, int]:
    """Compute a stable portrait/square-ish split pane box centered on a participant with headroom."""
    # Keep generous framing (avoid excessive zoom: at least 58% of source height if available).
    desired_h = max(int(src_height * 0.62), int(face.h * 3.4))
    box_h = min(src_height, max(120, desired_h))
    box_w = int(round(box_h * pane_aspect))
    if box_w > src_width:
        box_w = src_width
        box_h = int(round(box_w / max(0.1, pane_aspect)))
    box_w = max(2, box_w - (box_w % 2))
    box_h = max(2, box_h - (box_h % 2))
    # Position face slightly above vertical center (eyes around 38% from top) for natural headroom.
    target_y = face.cy - box_h * 0.38
    target_x = face.cx - box_w * 0.50
    x = int(max(0, min(src_width - box_w, round(target_x))))
    y = int(max(0, min(src_height - box_h, round(target_y))))
    return (x, y, box_w, box_h)


@dataclass(slots=True)
class _TrackState:
    track_id: int
    last_det: FaceDetection
    last_seen: float
    prev_mouth: np.ndarray | None = None
    prev_upper: np.ndarray | None = None
    activity_history: list[tuple[float, float]] = field(default_factory=list)
    last_lip_motion: float = 0.0
    smoothed_cx: float = 0.0
    hits: int = 1


@dataclass(slots=True)
class _FrameSample:
    rel_t: float
    is_cut: bool
    vad: float
    tracks: list[tuple[int, FaceDetection, float]]  # (track_id, detection, speaking_confidence)
    saliency_x: float
    lip_motions: dict[int, float] = field(default_factory=dict)
    turn_boundary: bool = False


def _plan_cache_path(
    source: Path, start: float, end: float, crop_width: int, mode: str, sample_fps: float,
    style: str = "natural",
    cache_dir: Path | None = None,
) -> Path | None:
    try:
        stat = source.stat()
    except OSError:
        return None
    key = f"reframe-v3:{source.resolve()}:{stat.st_size}:{stat.st_mtime_ns}:{start:.3f}:{end:.3f}:{crop_width}:{mode}:{style}:{sample_fps:.2f}"
    digest = hashlib.sha256(key.encode()).hexdigest()[:32]
    base_dir = cache_dir if cache_dir is not None else CACHE_DIR
    return base_dir / "tracking" / f"{digest}.json"


def _refine_exact_cut_frame(
    source: Path,
    prev_frame_idx: int,
    curr_frame_idx: int,
) -> int:
    """Pinpoint the exact native-FPS frame of a detected scene cut inside [prev_frame_idx+1, curr_frame_idx]."""
    if curr_frame_idx - prev_frame_idx <= 1:
        return curr_frame_idx
    cap = cv2.VideoCapture(str(source))
    if not cap.isOpened():
        return curr_frame_idx
    try:
        cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, prev_frame_idx))
        prev_g: np.ndarray | None = None
        best_frame = curr_frame_idx
        best_mad = -1.0
        for fidx in range(prev_frame_idx, curr_frame_idx + 1):
            ok, fr = cap.read()
            if not ok or fr is None:
                break
            g = cv2.cvtColor(cv2.resize(fr, (160, 90), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY)
            if prev_g is not None:
                mad = float(np.mean(np.abs(g.astype(np.float32) - prev_g.astype(np.float32))))
                if mad > best_mad:
                    best_mad = mad
                    best_frame = fidx
            prev_g = g
        return best_frame
    finally:
        cap.release()


def _analyze_clip_frames(
    source: Path,
    start: float,
    end: float,
    src_width: int,
    src_height: int,
    manager: ProcessManager,
    *,
    sample_fps: float = 5.0,
    transcript: Transcript | None = None,
) -> tuple[list[_FrameSample], str, float]:
    """Decode only the clip range (with 1s pre-roll), refine exact native-FPS cut frames, track faces, and score ASD."""
    pre_roll = min(1.0, max(0.0, start))
    scan_start = max(0.0, start - pre_roll)
    scan_end = end + 0.15
    vad_env, vad_bin = extract_audio_vad_envelope(source, scan_start, scan_end, transcript=transcript)

    # Collect transcript segment boundaries for conversational turn-aligned cuts
    turn_times: list[float] = []
    if transcript is not None and transcript.segments:
        for seg in transcript.segments:
            if seg.end >= start - 0.5 and seg.start <= end + 0.5:
                turn_times.append(seg.start - start)
                turn_times.append(seg.end - start)

    detector = _FaceDetector()
    capture = cv2.VideoCapture(str(source))
    samples: list[_FrameSample] = []
    fps = 25.0
    if not capture.isOpened():
        return samples, detector.backend, fps

    try:
        fps = float(capture.get(cv2.CAP_PROP_FPS) or 30.0)
        if not math.isfinite(fps) or fps <= 1.0 or fps > 240.0:
            fps = 30.0
        frame_stride = max(1, int(round(fps / max(1.0, sample_fps))))
        # Align clip_start_frame with FFmpeg `-ss {start:.3f}` + `setpts=PTS-STARTPTS`:
        # FFmpeg outputs the first frame whose PTS >= start!
        clip_start_frame = int(math.ceil(start * fps - 1e-6))
        start_frame = max(0, int(round(scan_start * fps)))
        end_frame = int(round(scan_end * fps))
        if start_frame > 0:
            capture.set(cv2.CAP_PROP_POS_FRAMES, start_frame)

        tracks: dict[int, _TrackState] = {}
        next_tid = 1
        prev_small: np.ndarray | None = None
        prev_sampled_frame_idx: int | None = None
        frame_idx = start_frame

        while frame_idx <= end_frame:
            manager.check_cancelled()
            ok = capture.grab()
            if not ok:
                break
            if (frame_idx - start_frame) % frame_stride != 0:
                frame_idx += 1
                continue
            ok, frame = capture.retrieve()
            if not ok or frame is None:
                break

            abs_t = frame_idx / fps
            rel_t = (frame_idx - clip_start_frame) / fps
            scale = min(1.0, 640.0 / max(1, frame.shape[1]))
            small_bgr = cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1.0 else frame
            small_gray = cv2.cvtColor(small_bgr, cv2.COLOR_BGR2GRAY)

            is_cut = _is_scene_cut(prev_small, small_gray)
            exact_cut_rel_t = rel_t
            if is_cut and prev_sampled_frame_idx is not None and frame_idx - prev_sampled_frame_idx > 1 and rel_t >= -0.05:
                exact_f = _refine_exact_cut_frame(source, prev_sampled_frame_idx, frame_idx)
                exact_cut_rel_t = max(0.0, (exact_f - clip_start_frame) / fps)
            prev_small = small_gray
            prev_sampled_frame_idx = frame_idx
            if is_cut:
                tracks.clear()

            dets = detector.detect(small_bgr, scale)
            vad_idx = min(len(vad_env) - 1, max(0, int((abs_t - scan_start) / vad_bin)))
            vad = float(vad_env[vad_idx]) if len(vad_env) else 0.5

            # Persistent track matching via IoU + normalized centroid distance.
            matched_tracks: set[int] = set()
            matched_dets: set[int] = set()
            pairs: list[tuple[float, int, int]] = []
            for tid, tr in tracks.items():
                for di, det in enumerate(dets):
                    iou_val = _iou(tr.last_det, det)
                    dist = math.hypot(tr.last_det.cx - det.cx, tr.last_det.cy - det.cy) / max(1.0, src_width)
                    if iou_val >= 0.15 or dist <= 0.14:
                        score = iou_val * 2.0 + max(0.0, 1.0 - dist * 5.0)
                        pairs.append((score, tid, di))
            pairs.sort(reverse=True)
            for _score, tid, di in pairs:
                if tid in matched_tracks or di in matched_dets:
                    continue
                matched_tracks.add(tid)
                matched_dets.add(di)
                tr = tracks[tid]
                det = dets[di]
                mouth_box, upper_box = _mouth_and_upper_boxes(det)
                mouth_patch = _extract_roi_patch(small_gray, mouth_box, scale)
                upper_patch = _extract_roi_patch(small_gray, upper_box, scale)
                raw_mouth = (
                    float(np.mean(np.abs(mouth_patch - tr.prev_mouth)))
                    if (mouth_patch is not None and tr.prev_mouth is not None) else 0.0
                )
                raw_upper = (
                    float(np.mean(np.abs(upper_patch - tr.prev_upper)))
                    if (upper_patch is not None and tr.prev_upper is not None) else 0.0
                )
                # Head-motion compensation: subtract rigid head movement measured on upper face.
                lip_motion = max(0.0, raw_mouth - 0.65 * raw_upper)
                # Synchronize with audio voice activity: suppress silent smiles/nods during silence.
                av_activity = lip_motion * (0.20 + 0.80 * vad) if vad >= 0.15 else lip_motion * 0.12
                tr.prev_mouth = mouth_patch
                tr.prev_upper = upper_patch
                tr.last_det = det
                tr.last_seen = abs_t
                tr.last_lip_motion = lip_motion
                tr.smoothed_cx = 0.45 * det.cx + 0.55 * tr.smoothed_cx
                tr.hits += 1
                tr.activity_history.append((abs_t, av_activity))
                tr.activity_history = [(ts, v) for ts, v in tr.activity_history if abs_t - ts <= 0.90]

            # Spawn tracks for unmatched detections.
            for di, det in enumerate(dets):
                if di in matched_dets:
                    continue
                mouth_box, upper_box = _mouth_and_upper_boxes(det)
                tracks[next_tid] = _TrackState(
                    track_id=next_tid,
                    last_det=det,
                    last_seen=abs_t,
                    prev_mouth=_extract_roi_patch(small_gray, mouth_box, scale),
                    prev_upper=_extract_roi_patch(small_gray, upper_box, scale),
                    activity_history=[(abs_t, 0.0)],
                    smoothed_cx=det.cx,
                )
                next_tid += 1

            # Drop expired tracks (0.85s dropout tolerance preserves stability during brief turns/occlusions).
            expired = [tid for tid, tr in tracks.items() if abs_t - tr.last_seen > 0.85]
            for tid in expired:
                del tracks[tid]

            # Record samples inside actual clip range [0.0, end - start].
            sample_rel_t = exact_cut_rel_t if is_cut else rel_t
            if sample_rel_t >= -0.05:
                clamped_t = max(0.0, sample_rel_t)
                frame_tracks: list[tuple[int, FaceDetection, float]] = []
                lip_map: dict[int, float] = {}
                for tid, tr in tracks.items():
                    if abs_t - tr.last_seen > 0.65:
                        continue
                    recent = [v for _, v in tr.activity_history]
                    mean_act = sum(recent) / max(1, len(recent))
                    # Combine temporal mouth activity + subtle face-size centrality weight (tie-breaker only).
                    size_weight = min(0.14, math.sqrt(tr.last_det.area / max(1.0, src_width * src_height)) * 0.38)
                    confidence = mean_act + size_weight * (0.35 + 0.65 * vad)
                    frame_tracks.append((tid, tr.last_det, confidence))
                    lip_map[tid] = tr.last_lip_motion
                saliency_x = _content_saliency_center_x(small_gray, src_width) if not frame_tracks else src_width * 0.5
                is_turn = any(abs(clamped_t - tt) <= 0.22 for tt in turn_times)
                samples.append(_FrameSample(
                    rel_t=clamped_t,
                    is_cut=is_cut,
                    vad=vad,
                    tracks=frame_tracks,
                    saliency_x=saliency_x,
                    lip_motions=lip_map,
                    turn_boundary=is_turn,
                ))
            frame_idx += 1
    finally:
        capture.release()
    return samples, detector.backend, fps


def _samples_to_director_inputs(samples: list[_FrameSample]) -> list[DirectorSample]:
    return [
        DirectorSample(
            rel_t=s.rel_t,
            is_cut=s.is_cut,
            vad=s.vad,
            tracks=[
                TrackObservation(
                    track_id=tid,
                    x=det.x,
                    y=det.y,
                    w=det.w,
                    h=det.h,
                    speaking_conf=conf,
                    lip_motion=float(s.lip_motions.get(tid, 0.0)),
                    det_score=det.score,
                )
                for tid, det, conf in s.tracks
            ],
            saliency_x=s.saliency_x,
            turn_boundary=s.turn_boundary,
        )
        for s in samples
    ]


def _build_single_track_keyframes(
    samples: list[_FrameSample],
    src_width: int,
    crop_width: int,
    *,
    mode: str,
    metrics: FramingMetrics,
    style: str = "natural",
    src_height: int = 1080,
    source_fps: float = 25.0,
    min_hold_seconds: float | None = None,
    hysteresis_ratio: float | None = None,
) -> list[CropKeyframe]:
    """Delegate tracked multi-person samples to the offline Auto Director for cinematic crop keyframes."""
    if not samples:
        return []
    _ = min_hold_seconds, hysteresis_ratio  # Preserved for backward-compatible signature
    dir_samples = _samples_to_director_inputs(samples)
    directed, stats = direct_single_stream(
        dir_samples,
        src_width,
        src_height,
        crop_width,
        mode=mode,
        style=style,
        source_fps=source_fps,
    )
    metrics.scene_cuts += stats.original_cuts
    metrics.speaker_switches += stats.artificial_switches
    metrics.midpoint_avoided_frames += stats.midpoint_avoided_frames
    metrics.style_used = stats.style_used
    metrics.median_shot_duration = stats.median_shot_duration
    metrics.crop_jitter_px_s = stats.crop_jitter_px_s
    metrics.face_visibility_ratio = stats.face_visibility_ratio
    return [CropKeyframe(kf.time, kf.left, kf.cut) for kf in directed]


def _build_scene_adaptive_plan(
    samples: list[_FrameSample],
    duration: float,
    src_width: int,
    src_height: int,
    crop_width: int,
    output_size: tuple[int, int],
    *,
    mode: str,
    metrics: FramingMetrics,
    style: str = "natural",
    source_fps: float = 25.0,
) -> FramingPlan:
    """Build single-speaker portrait timeline, or explicit Podcast Split Screen <-> Close-up segments."""
    if not samples:
        fb = _clamp_crop_left(src_width * 0.5, crop_width, src_width)
        return FramingPlan(
            segments=[FramingSegment("single", 0.0, duration, [CropKeyframe(0.0, fb)])],
            metrics=metrics,
            has_split=False,
        )

    # Automatic / single-stream modes strictly stay in single full-height 9:16 portrait framing.
    # Podcast Split Screen is activated only when explicitly requested via mode == "split_screen".
    if mode != "split_screen":
        metrics.single_segments = 1
        kfs = _build_single_track_keyframes(
            samples, src_width, crop_width, mode=mode, metrics=metrics,
            style=style, src_height=src_height, source_fps=source_fps,
        )
        return FramingPlan(
            segments=[FramingSegment("single", 0.0, duration, keyframes=kfs)],
            metrics=metrics,
            has_split=False,
        )

    # Explicit Podcast Split Screen: split at camera cuts so close-up shots return to single-speaker framing.
    scenes: list[list[_FrameSample]] = [[]]
    for sample in samples:
        if sample.is_cut and scenes[-1]:
            scenes.append([])
        scenes[-1].append(sample)

    pane_aspect = output_size[0] / max(1.0, output_size[1] * 0.5)
    segments: list[FramingSegment] = []

    for scene_idx, scene_samples in enumerate(scenes):
        seg_start = 0.0 if scene_idx == 0 else scene_samples[0].rel_t
        seg_end = duration if scene_idx == len(scenes) - 1 else scenes[scene_idx + 1][0].rel_t
        if seg_end - seg_start < 0.08:
            continue

        distant_two_frames = 0
        left_faces: list[FaceDetection] = []
        right_faces: list[FaceDetection] = []
        for s in scene_samples:
            dets = [d for _, d, _ in s.tracks]
            if len(dets) >= 2:
                fits, _ = can_group_frame(dets, crop_width)
                if not fits:
                    distant_two_frames += 1
                    ordered = sorted(dets, key=lambda d: d.cx)
                    left_faces.append(ordered[0])
                    right_faces.append(ordered[-1])

        two_shot_ratio = distant_two_frames / max(1, len(scene_samples))
        use_split = (
            two_shot_ratio >= 0.50
            and (seg_end - seg_start) >= 0.80
            and bool(left_faces)
            and bool(right_faces)
        )

        if use_split:
            # Median participant positions keep split-screen rock-steady with left host on top, right on bottom.
            med_left = FaceDetection(
                float(np.median([f.x for f in left_faces])),
                float(np.median([f.y for f in left_faces])),
                float(np.median([f.w for f in left_faces])),
                float(np.median([f.h for f in left_faces])),
            )
            med_right = FaceDetection(
                float(np.median([f.x for f in right_faces])),
                float(np.median([f.y for f in right_faces])),
                float(np.median([f.w for f in right_faces])),
                float(np.median([f.h for f in right_faces])),
            )
            top_box = compute_split_box(med_left, src_width, src_height, pane_aspect)
            bottom_box = compute_split_box(med_right, src_width, src_height, pane_aspect)
            metrics.split_segments += 1
            segments.append(FramingSegment("split", seg_start, seg_end, top_box=top_box, bottom_box=bottom_box))
        else:
            shifted = [
                _FrameSample(
                    max(0.0, s.rel_t - seg_start), False, s.vad, s.tracks, s.saliency_x,
                    lip_motions=s.lip_motions, turn_boundary=s.turn_boundary,
                )
                for s in scene_samples
            ]
            kfs = _build_single_track_keyframes(
                shifted, src_width, crop_width, mode=mode, metrics=metrics,
                style=style, src_height=src_height, source_fps=source_fps,
            )
            metrics.single_segments += 1
            segments.append(FramingSegment("single", seg_start, seg_end, keyframes=kfs))

    # Merge consecutive single segments if no split exists (single-pass FFmpeg filter is faster!).
    has_split = any(s.kind == "split" for s in segments)
    if not has_split:
        metrics.scene_cuts = 0
        metrics.speaker_switches = 0
        metrics.midpoint_avoided_frames = 0
        metrics.single_segments = 1
        kfs = _build_single_track_keyframes(
            samples, src_width, crop_width, mode=mode, metrics=metrics,
            style=style, src_height=src_height, source_fps=source_fps,
        )
        return FramingPlan(
            segments=[FramingSegment("single", 0.0, duration, keyframes=kfs)],
            metrics=metrics,
            has_split=False,
        )
    return FramingPlan(segments=segments, metrics=metrics, has_split=True)


def analyze_smart_reframe(
    source: Path,
    start: float,
    end: float,
    src_width: int,
    src_height: int,
    crop_width: int,
    output_size: tuple[int, int],
    manager: ProcessManager,
    log: Callable[[str], None],
    *,
    mode: str = "auto",
    style: str = "natural",
    sample_interval: float = 0.75,
    cache_enabled: bool = False,
    transcript: Transcript | None = None,
    cache_dir: Path | None = None,
) -> FramingPlan:
    """Analyze a clip range and build a deterministic, cached Smart Reframing plan."""
    selected_mode = mode if mode in VALID_REFRAME_MODES else "auto"
    selected_style = get_style_config(style).name
    duration = max(0.1, end - start)

    if selected_mode == "fit_original":
        metrics = FramingMetrics(mode_used="fit_original", style_used=selected_style, detector_backend="none")
        log("Smart Reframing: Fit Original Video (blurred vertical background)")
        return FramingPlan(segments=[FramingSegment("fit", 0.0, duration)], metrics=metrics, has_split=False)

    if selected_mode == "off" or crop_width >= src_width:
        fb = _clamp_crop_left(src_width * 0.5, crop_width, src_width)
        metrics = FramingMetrics(mode_used="off", style_used=selected_style, detector_backend="none", single_segments=1)
        return FramingPlan(
            segments=[FramingSegment("single", 0.0, duration, [CropKeyframe(0.0, fb)])],
            metrics=metrics,
            has_split=False,
        )

    # Adaptive sampling FPS respects Eco/Balanced/Turbo sample_interval while staying fast enough for ASD.
    if selected_mode == "single_subject":
        sample_fps = max(1.5, min(4.0, 1.0 / max(0.25, sample_interval)))
    else:
        # Eco (1.0s) -> 4 fps, Balanced (0.75s) -> 5 fps, Turbo (0.5s) -> 6 fps
        sample_fps = 6.0 if sample_interval <= 0.55 else (5.0 if sample_interval <= 0.85 else 4.0)

    cache_path = (
        _plan_cache_path(source, start, end, crop_width, selected_mode, sample_fps, style=selected_style, cache_dir=cache_dir)
        if cache_enabled else None
    )
    if cache_path is not None and cache_path.is_file():
        try:
            plan = FramingPlan.from_dict(json.loads(cache_path.read_text(encoding="utf-8")))
            if plan.segments and (selected_mode == "split_screen" or not plan.has_split):
                log(f"Using cached face tracking / Auto Director plan ({selected_mode}/{selected_style}, {len(plan.segments)} segment(s))")
                return plan
        except (OSError, ValueError, KeyError, TypeError):
            pass

    samples, backend, source_fps = _analyze_clip_frames(
        source, start, end, src_width, src_height, manager,
        sample_fps=sample_fps, transcript=transcript,
    )
    metrics = FramingMetrics(
        mode_used=selected_mode,
        style_used=selected_style,
        detector_backend=backend,
        frames_analyzed=len(samples),
    )
    plan = _build_scene_adaptive_plan(
        samples, duration, src_width, src_height, crop_width, output_size,
        mode=selected_mode, style=selected_style, metrics=metrics, source_fps=source_fps,
    )
    if cache_path is not None and plan.segments:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(plan.to_dict()), encoding="utf-8")
        except OSError:
            pass
    log(
        f"Auto Director ({selected_mode}/{selected_style}, {backend}): {metrics.frames_analyzed} frames, "
        f"{metrics.speaker_switches} switch(es), {metrics.scene_cuts} source cut(s), "
        f"median shot {metrics.median_shot_duration:.1f}s, jitter {metrics.crop_jitter_px_s:.1f} px/s"
        + (f", {metrics.split_segments} split segment(s)" if plan.has_split else "")
    )
    return plan


def keyframes_to_crop_x_expression(keyframes: list[CropKeyframe], fallback: float) -> str:
    """Build a balanced FFmpeg x-expression supporting BOTH smooth pans and frame-accurate instant cuts."""
    if not keyframes:
        return f"{fallback:.2f}"
    # Compress redundant stationary runs to keep FFmpeg expression depth compact.
    compact: list[CropKeyframe] = [keyframes[0]]
    for kf in keyframes[1:]:
        prev = compact[-1]
        if not kf.cut and abs(kf.left - prev.left) < 0.75 and len(compact) >= 2 and abs(compact[-2].left - prev.left) < 0.75 and not prev.cut:
            compact[-1] = kf
        else:
            compact.append(kf)

    def _fmt_t(val: float) -> str:
        return f"{val:.2f}" if abs(val * 100.0 - round(val * 100.0)) < 1e-4 else f"{val:.3f}"

    def piecewise(first: int, last: int) -> str:
        if first == last:
            kf_a = compact[first]
            if first == len(compact) - 1:
                return f"{kf_a.left:.0f}"
            kf_b = compact[first + 1]
            # Instant step cut: hold kf_a.left cleanly until kf_b.time (no empty-space pan!).
            if kf_b.cut or abs(kf_b.left - kf_a.left) < 0.5:
                return f"{kf_a.left:.2f}"
            span = max(0.01, kf_b.time - kf_a.time)
            return f"{kf_a.left:.2f}+({kf_b.left - kf_a.left:.2f})*(t-{_fmt_t(kf_a.time)})/{_fmt_t(span)}"
        middle = (first + last + 1) // 2
        t_mid = compact[middle].time
        earlier = piecewise(first, middle - 1)
        later = piecewise(middle, last)
        return f"if(lt(t,{_fmt_t(t_mid)}),{earlier},{later})"

    return piecewise(0, len(compact) - 1)
