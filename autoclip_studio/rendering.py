from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from tempfile import mkstemp
from typing import Callable

from .captions import write_ass
from .media import probe, validate_mp4
from .models import Highlight, Transcript
from .performance import ProfileLimits
from .process import ProcessManager, find_media_tool
from .smart_reframe import FramingPlan, FramingSegment, analyze_smart_reframe, keyframes_to_crop_x_expression
from .tracking import crop_x_expression, track_faces


def _parse_rate(value: object) -> float | None:
    if not isinstance(value, str) or "/" not in value:
        return None
    left, right = value.split("/", 1)
    try:
        num, den = float(left), float(right)
    except ValueError:
        return None
    if den <= 0 or num <= 0:
        return None
    fps = num / den
    return fps if 5.0 <= fps <= 240.0 else None


def _stream_metadata(
    source: Path,
    manager: ProcessManager,
    dimensions: tuple[int, int] | None,
) -> tuple[tuple[int, int], float | None, bool, bool]:
    """Return ((width, height), fps, is_vfr, has_audio) cleanly, tolerating synthetic/mocked calls."""
    info: dict | None = None
    if dimensions is None:
        info = probe(source, manager)
    elif source.is_file():
        try:
            info = probe(source, manager)
        except Exception:
            info = None
    if not info:
        return dimensions or (0, 0), None, False, False

    streams = info.get("streams", [])
    video = next((item for item in streams if item.get("codec_type") == "video"), None)
    has_audio = any(item.get("codec_type") == "audio" for item in streams)
    if dimensions is None:
        if not video:
            raise RuntimeError("The source has no video stream.")
        width, height = int(video["width"]), int(video["height"])
        for side_data in video.get("side_data_list", []):
            rotation = side_data.get("rotation", 0)
            if abs(rotation) % 180 == 90:
                width, height = height, width
                break
        dims = (width, height)
    else:
        dims = dimensions

    avg_fps = _parse_rate(video.get("avg_frame_rate") if video else None)
    r_fps = _parse_rate(video.get("r_frame_rate") if video else None)
    fps = avg_fps or r_fps
    is_vfr = bool(avg_fps and r_fps and abs(r_fps - avg_fps) > 1.5)
    return dims, fps, is_vfr, has_audio


def source_dimensions(source: Path, manager: ProcessManager) -> tuple[int, int]:
    info = probe(source, manager)
    video = next((item for item in info.get("streams", []) if item.get("codec_type") == "video"), None)
    if not video:
        raise RuntimeError("The source has no video stream.")
    width, height = int(video["width"]), int(video["height"])
    for side_data in video.get("side_data_list", []):
        rotation = side_data.get("rotation", 0)
        if abs(rotation) % 180 == 90:
            return height, width
    return width, height


_ENCODER_PROBE_CACHE: dict[tuple[str, str], bool] = {}


def _build_filter_args(
    plan: FramingPlan,
    crop_width: int,
    crop_height: int,
    y: int,
    x_fallback: float,
    output_size: tuple[int, int],
    *,
    captions: bool,
    vfr_fps: float | None = None,
) -> tuple[list[str], str]:
    """Build FFmpeg -vf or -filter_complex args + mapped video stream label."""
    out_w, out_h = output_size
    sub_suffix = ",subtitles=captions.ass" if captions else ""
    pts_prefix = f"fps={vfr_fps:.6g}:round=near,setpts=PTS-STARTPTS" if vfr_fps else "setpts=PTS-STARTPTS"

    # Fast single-segment single-crop path (normalize PTS to 0.000s so crop t & subtitles stay aligned)
    if len(plan.segments) == 1 and plan.segments[0].kind == "single":
        seg = plan.segments[0]
        x_expr = keyframes_to_crop_x_expression(seg.keyframes, x_fallback).replace(",", "\\,")
        vf = f"{pts_prefix},crop={crop_width}:{crop_height}:'{x_expr}':{y},scale={out_w}:{out_h}:flags=lanczos{sub_suffix}"
        return ["-vf", vf], "0:v:0"

    # Fit Original Video with blurred vertical background
    if len(plan.segments) == 1 and plan.segments[0].kind == "fit":
        fc = (
            f"[0:v]{pts_prefix},split=2[bg][fg];"
            f"[bg]scale={out_w}:{out_h}:force_original_aspect_ratio=increase,crop={out_w}:{out_h},gblur=sigma=24[b];"
            f"[fg]scale={out_w}:{out_h}:force_original_aspect_ratio=decrease[f];"
            f"[b][f]overlay=(W-w)/2:(H-h)/2,setsar=1{sub_suffix}[vout]"
        )
        return ["-filter_complex", fc], "[vout]"

    # Single-segment Podcast Split Screen
    half_h = (out_h // 2) - ((out_h // 2) % 2)
    bot_h = out_h - half_h
    if len(plan.segments) == 1 and plan.segments[0].kind == "split" and plan.segments[0].top_box and plan.segments[0].bottom_box:
        tx, ty, tw, th = plan.segments[0].top_box
        bx, by, bw, bh = plan.segments[0].bottom_box
        fc = (
            f"[0:v]{pts_prefix},split=2[topin][botin];"
            f"[topin]crop={tw}:{th}:{tx}:{ty},scale={out_w}:{half_h}:flags=lanczos,setsar=1[top];"
            f"[botin]crop={bw}:{bh}:{bx}:{by},scale={out_w}:{bot_h}:flags=lanczos,setsar=1[bot];"
            f"[top][bot]vstack=inputs=2,setsar=1{sub_suffix}[vout]"
        )
        return ["-filter_complex", fc], "[vout]"

    # Multi-segment mixed timeline (e.g., Split Screen <-> Single-Speaker Close-Up cuts)
    n = len(plan.segments)
    parts = [f"[0:v]{pts_prefix},split={n}" + "".join(f"[s{i}]" for i in range(n))]
    out_labels: list[str] = []
    for i, seg in enumerate(plan.segments):
        trim = f"trim=start={seg.start:.3f}:end={seg.end:.3f},setpts=PTS-STARTPTS"
        if seg.kind == "split" and seg.top_box and seg.bottom_box:
            tx, ty, tw, th = seg.top_box
            bx, by, bw, bh = seg.bottom_box
            parts.append(f"[s{i}]{trim},split=2[st{i}][sb{i}]")
            parts.append(f"[st{i}]crop={tw}:{th}:{tx}:{ty},scale={out_w}:{half_h}:flags=lanczos,setsar=1[top{i}]")
            parts.append(f"[sb{i}]crop={bw}:{bh}:{bx}:{by},scale={out_w}:{bot_h}:flags=lanczos,setsar=1[bot{i}]")
            parts.append(f"[top{i}][bot{i}]vstack=inputs=2,setsar=1[v{i}]")
        else:
            x_expr = keyframes_to_crop_x_expression(seg.keyframes, x_fallback).replace(",", "\\,")
            parts.append(f"[s{i}]{trim},crop={crop_width}:{crop_height}:'{x_expr}':{y},scale={out_w}:{out_h}:flags=lanczos,setsar=1[v{i}]")
        out_labels.append(f"[v{i}]")
    parts.append(f"{''.join(out_labels)}concat=n={n}:v=1:a=0,setsar=1{sub_suffix}[vout]")
    return ["-filter_complex", ";".join(parts)], "[vout]"


def render_clip(source: Path, highlight: Highlight, transcript: Transcript,
                output: Path, work_dir: Path, output_size: tuple[int, int],
                aspect_ratio: str, face_tracking: bool, captions: bool,
                caption_template: str, hardware: bool, manager: ProcessManager,
                log: Callable[[str], None], *,
                dimensions: tuple[int, int] | None = None,
                encoder_support: dict[str, bool] | None = None,
                profile_limits: ProfileLimits | None = None,
                cache_enabled: bool = False,
                video_codec: str = "h264",
                reframe_mode: str | None = None,
                reframe_style: str = "natural",
                caption_sync_mode: str = "precise",
                caption_offset_ms: int = 0) -> float:
    work_dir.mkdir(parents=True, exist_ok=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    (src_width, src_height), source_fps, is_vfr, has_audio = _stream_metadata(source, manager, dimensions)
    ratio_left, ratio_right = (float(item) for item in aspect_ratio.split(":", 1))
    target_ratio = ratio_left / ratio_right
    if src_width / src_height > target_ratio:
        crop_height = src_height - (src_height % 2)
        crop_width = int(crop_height * target_ratio) // 2 * 2
    else:
        crop_width = src_width - (src_width % 2)
        crop_height = int(crop_width / target_ratio) // 2 * 2
    x_fallback = max(0, (src_width - crop_width) / 2)
    y = max(0, (src_height - crop_height) // 2)

    effective_mode = reframe_mode if reframe_mode is not None else ("auto" if face_tracking else "off")
    if not face_tracking and reframe_mode is None:
        effective_mode = "off"

    interval = profile_limits.tracking_sample_interval if profile_limits else 0.75
    if effective_mode in {"off"} and not face_tracking:
        plan = FramingPlan(segments=[FramingSegment("single", 0.0, max(0.1, highlight.end - highlight.start))])
    else:
        plan = analyze_smart_reframe(
            source, highlight.start, highlight.end, src_width, src_height,
            crop_width, output_size, manager, log,
            mode=effective_mode, style=reframe_style, sample_interval=interval,
            cache_enabled=cache_enabled, transcript=transcript,
        )

    if captions:
        ass_path = work_dir / "captions.ass"
        write_ass(
            transcript, highlight, ass_path, output_size, caption_template,
            split_screen=plan.has_split,
            fps=source_fps,
            sync_mode=caption_sync_mode,
            offset_ms=caption_offset_ms,
        )

    vfr_fps = source_fps if is_vfr and source_fps else None
    filter_args, video_map = _build_filter_args(
        plan, crop_width, crop_height, y, x_fallback, output_size,
        captions=captions, vfr_fps=vfr_fps,
    )
    duration = highlight.end - highlight.start
    thread_args = ["-threads", str(profile_limits.ffmpeg_threads)] if profile_limits else []
    audio_args = ["-af", "asetpts=PTS-STARTPTS"] if has_audio else []
    base = [
        find_media_tool("ffmpeg"), "-hide_banner", "-nostats", "-progress", "pipe:1",
        "-stats_period", "0.5", "-y", *thread_args, "-ss", f"{highlight.start:.3f}",
        "-i", str(source), "-t", f"{duration:.3f}", *filter_args,
        "-map", video_map, "-map", "0:a?", *audio_args, "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart", "-pix_fmt", "yuv420p",
    ]
    descriptor, staging_path = mkstemp(prefix=".render-", suffix=".mp4", dir=output.parent)
    os.close(descriptor)
    staging = Path(staging_path)
    try:
        encoder = "cpu"
        if hardware:
            if encoder_support is None:
                encoder = detect_hw_encoder(manager)
            else:
                key = f"{base[0]}:{sys.platform}"
                if key not in encoder_support:
                    encoder_support[key] = detect_hw_encoder(manager)
                encoder = str(encoder_support[key])
        nvenc_preset = profile_limits.nvenc_preset if profile_limits else "p5"
        cpu_preset = profile_limits.cpu_preset if profile_limits else "medium"
        if encoder == "nvenc":
            command = base + ["-c:v", "h264_nvenc", "-preset", nvenc_preset, "-cq", "21", str(staging)]
            try:
                log(f"Rendering with NVIDIA NVENC ({nvenc_preset})")
                manager.run(command, cwd=work_dir, on_output=_ffmpeg_logger(log), output_tail=50)
            except subprocess.CalledProcessError as error:
                log(f"NVENC failed; retrying with CPU encoder ({_last_line(error.output)})")
                _render_cpu(base, staging, work_dir, manager, log, preset=cpu_preset)
        elif encoder == "videotoolbox":
            use_hevc = video_codec.lower() in {"hevc", "h265"} and hevc_videotoolbox_available(manager)
            vt_args = (
                ["-c:v", "hevc_videotoolbox", "-q:v", "65", "-tag:v", "hvc1"]
                if use_hevc else ["-c:v", "h264_videotoolbox", "-q:v", "65"]
            )
            vt_name = "hevc_videotoolbox" if use_hevc else "h264_videotoolbox"
            command = base + vt_args + [str(staging)]
            try:
                log(f"Rendering with Apple VideoToolbox ({vt_name})")
                manager.run(command, cwd=work_dir, on_output=_ffmpeg_logger(log), output_tail=50)
            except subprocess.CalledProcessError as error:
                log(f"VideoToolbox failed; retrying with CPU encoder ({_last_line(error.output)})")
                _render_cpu(base, staging, work_dir, manager, log, preset=cpu_preset)
        else:
            log(f"Rendering with CPU encoder ({cpu_preset})")
            _render_cpu(base, staging, work_dir, manager, log, preset=cpu_preset)
        rendered_duration = validate_mp4(staging, manager)
        manager.check_cancelled()
        staging.replace(output)
        return rendered_duration
    finally:
        staging.unlink(missing_ok=True)


def _render_cpu(base: list[str], output: Path, work_dir: Path,
                manager: ProcessManager, log: Callable[[str], None], *, preset: str = "medium") -> None:
    manager.run(
        base + ["-c:v", "libx264", "-preset", preset, "-crf", "20", str(output)],
        cwd=work_dir, on_output=_ffmpeg_logger(log), output_tail=50,
    )


def _probe_encoder(manager: ProcessManager, codec: str, extra_args: tuple[str, ...] = ()) -> bool:
    try:
        ffmpeg = find_media_tool("ffmpeg")
    except FileNotFoundError:
        return False
    cache_key = (ffmpeg, codec)
    if cache_key in _ENCODER_PROBE_CACHE:
        return _ENCODER_PROBE_CACHE[cache_key]
    try:
        manager.run([
            ffmpeg, "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=size=640x480:rate=30", "-frames:v", "1",
            "-pix_fmt", "yuv420p", "-c:v", codec, *extra_args,
            "-f", "null", "-",
        ], output_tail=20)
        ok = True
    except (OSError, subprocess.CalledProcessError):
        ok = False
    _ENCODER_PROBE_CACHE[cache_key] = ok
    return ok


def nvenc_available(manager: ProcessManager) -> bool:
    if sys.platform == "darwin":
        return False
    return _probe_encoder(manager, "h264_nvenc", ("-preset", "p5"))


def videotoolbox_available(manager: ProcessManager) -> bool:
    if sys.platform != "darwin":
        return False
    return _probe_encoder(manager, "h264_videotoolbox")


def hevc_videotoolbox_available(manager: ProcessManager) -> bool:
    if sys.platform != "darwin":
        return False
    return _probe_encoder(manager, "hevc_videotoolbox")


def detect_hw_encoder(manager: ProcessManager) -> str:
    if sys.platform == "darwin":
        return "videotoolbox" if videotoolbox_available(manager) else "cpu"
    return "nvenc" if nvenc_available(manager) else "cpu"


def _ffmpeg_logger(log: Callable[[str], None]) -> Callable[[str], None]:
    def emit(line: str) -> None:
        if line.startswith("out_time="):
            timestamp = line.partition("=")[2]
            if timestamp != "N/A":
                log(f"Encoded {timestamp.split('.')[0]}")
        elif "Error" in line or "failed" in line.lower():
            log(line.strip())
    return emit


def _last_line(value: str | None) -> str:
    lines = (value or "").splitlines()
    return lines[-1] if lines else "unknown encoder error"

