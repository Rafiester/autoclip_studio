from __future__ import annotations

import os
import subprocess
from pathlib import Path
from tempfile import mkstemp
from typing import Callable

from .captions import write_ass
from .media import probe, validate_mp4
from .models import Highlight, Transcript
from .process import ProcessManager, find_media_tool
from .tracking import crop_x_expression, track_faces


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


def render_clip(source: Path, highlight: Highlight, transcript: Transcript,
                output: Path, work_dir: Path, output_size: tuple[int, int],
                aspect_ratio: str, face_tracking: bool, captions: bool,
                caption_template: str, hardware: bool, manager: ProcessManager,
                log: Callable[[str], None], *,
                dimensions: tuple[int, int] | None = None,
                encoder_support: dict[str, bool] | None = None) -> float:
    work_dir.mkdir(parents=True, exist_ok=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    src_width, src_height = dimensions or source_dimensions(source, manager)
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
    points = []
    if face_tracking and crop_width < src_width:
        points = track_faces(source, highlight.start, highlight.end, crop_width, manager, log)
    x_expression = crop_x_expression(points, x_fallback).replace(",", "\\,")
    filters = [
        f"crop={crop_width}:{crop_height}:'{x_expression}':{y}",
        f"scale={output_size[0]}:{output_size[1]}:flags=lanczos",
    ]
    if captions:
        ass_path = work_dir / "captions.ass"
        write_ass(transcript, highlight, ass_path, output_size, caption_template)
        filters.append("subtitles=captions.ass")
    duration = highlight.end - highlight.start
    base = [
        find_media_tool("ffmpeg"), "-hide_banner", "-nostats", "-progress", "pipe:1",
        "-stats_period", "0.5", "-y", "-ss", f"{highlight.start:.3f}",
        "-i", str(source), "-t", f"{duration:.3f}", "-vf", ",".join(filters),
        "-map", "0:v:0", "-map", "0:a?", "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart", "-pix_fmt", "yuv420p",
    ]
    descriptor, staging_path = mkstemp(prefix=".render-", suffix=".mp4", dir=output.parent)
    os.close(descriptor)
    staging = Path(staging_path)
    try:
        can_use_nvenc = False
        if hardware:
            if encoder_support is None:
                can_use_nvenc = nvenc_available(manager)
            else:
                if base[0] not in encoder_support:
                    encoder_support[base[0]] = nvenc_available(manager)
                can_use_nvenc = encoder_support[base[0]]
        if can_use_nvenc:
            command = base + ["-c:v", "h264_nvenc", "-preset", "p5", "-cq", "21", str(staging)]
            try:
                log("Rendering with NVIDIA NVENC")
                manager.run(command, cwd=work_dir, on_output=_ffmpeg_logger(log), output_tail=50)
            except subprocess.CalledProcessError as error:
                log(f"NVENC failed; retrying with CPU encoder ({_last_line(error.output)})")
                _render_cpu(base, staging, work_dir, manager, log)
        else:
            log("Rendering with CPU encoder")
            _render_cpu(base, staging, work_dir, manager, log)
        rendered_duration = validate_mp4(staging, manager)
        manager.check_cancelled()
        staging.replace(output)
        return rendered_duration
    finally:
        staging.unlink(missing_ok=True)


def _render_cpu(base: list[str], output: Path, work_dir: Path,
                manager: ProcessManager, log: Callable[[str], None]) -> None:
    manager.run(
        base + ["-c:v", "libx264", "-preset", "medium", "-crf", "20", str(output)],
        cwd=work_dir, on_output=_ffmpeg_logger(log), output_tail=50,
    )


def nvenc_available(manager: ProcessManager) -> bool:
    try:
        manager.run([
            find_media_tool("ffmpeg"), "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=size=640x480:rate=30", "-frames:v", "1",
            "-pix_fmt", "yuv420p", "-c:v", "h264_nvenc", "-preset", "p5",
            "-f", "null", "-",
        ], output_tail=20)
    except (OSError, subprocess.CalledProcessError):
        return False
    return True


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

