from __future__ import annotations

import hashlib
import re
from pathlib import Path
from tempfile import TemporaryDirectory

from .captions import TEMPLATES, write_ass
from .models import Highlight, Segment, Transcript, Word
from .paths import CACHE_DIR
from .process import ProcessManager, find_media_tool


PREVIEW_DURATION = 6.6
PREVIEW_FRAME = (480, 854)
PREVIEW_BAND = (480, 270)

_SAMPLE_LINES = (
    ((.22, .57, "The"), (.59, 1.12, "hook"), (1.23, 1.83, "matters.")),
    ((2.12, 2.51, "Give"), (2.52, 2.85, "each"), (2.89, 3.54, "word"), (3.64, 4.15, "room.")),
    ((4.53, 4.86, "Make"), (4.89, 5.08, "the"), (5.13, 5.68, "ending"), (5.74, 6.23, "count.")),
)


def _sample() -> Transcript:
    return Transcript(PREVIEW_DURATION, "en", [
        Segment(line[0][0], line[-1][1], " ".join(word[2] for word in line), [Word(*word) for word in line])
        for line in _SAMPLE_LINES
    ])


def render_preview(name: str, manager: ProcessManager, cache: Path | None = None) -> Path:
    template = TEMPLATES[name]
    cache = cache if cache is not None else CACHE_DIR / "caption-previews"
    cache.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[^a-z0-9]+", "-", name.lower())
    executable = find_media_tool("ffmpeg")
    tool = Path(executable).stat()
    with TemporaryDirectory(prefix="caption-preview-", dir=cache) as temporary:
        work = Path(temporary)
        captions = work / "captions.ass"
        write_ass(_sample(), Highlight(0, PREVIEW_DURATION, name, "", "", 0), captions, PREVIEW_FRAME, name)
        key = hashlib.sha256(captions.read_bytes())
        key.update(f"band-v1:{template.background}:{PREVIEW_BAND}:{tool.st_size}:{tool.st_mtime_ns}".encode())
        destination = cache / f"{stem}-{key.hexdigest()[:16]}.mp4"
        manager.check_cancelled()
        if destination.is_file() and destination.stat().st_size > 1024:
            return destination
        staged = work / "preview.mp4"
        width, height = PREVIEW_FRAME
        band_width, band_height = PREVIEW_BAND
        band_top = height - round(height * .14) - 196
        command = [
            executable, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-filter_threads", "1",
            "-f", "lavfi", "-i", f"color=c=0x{template.background[1:]}:s={width}x{height}:r=24:d={PREVIEW_DURATION}",
            "-vf", f"subtitles=captions.ass,crop={band_width}:{band_height}:0:{band_top}",
            "-an", "-c:v", "libx264", "-threads", "2", "-preset", "ultrafast", "-crf", "20",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(staged),
        ]
        try:
            manager.run(command, cwd=work, output_tail=20)
        except Exception as error:
            # ProcessManager has already reaped the child if Windows denied job assignment.
            if (getattr(error, "funcname", "") != "AssignProcessToJobObject"
                    or getattr(error, "winerror", None) != 5):
                raise
            manager.run(command, cwd=work, output_tail=20)
        manager.check_cancelled()
        try:
            staged.replace(destination)
        except PermissionError:
            if not destination.is_file():
                raise
        for previous in cache.glob(f"{stem}-*.mp4"):
            if previous != destination:
                try:
                    previous.unlink()
                except OSError:
                    pass
        return destination
