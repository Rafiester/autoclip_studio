"""Manual end-to-end media smoke test used by the setup validation workflow."""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from autoclip_studio.media import validate_mp4
from autoclip_studio.models import Highlight, Segment, Transcript, Word
from autoclip_studio.process import ProcessManager, find_media_tool
from autoclip_studio.rendering import render_clip


def main() -> None:
    manager = ProcessManager()
    root = Path(tempfile.mkdtemp(prefix="autoclip-smoke-"))
    try:
        source = root / "source.mp4"
        output = root / "short.mp4"
        manager.run([
            find_media_tool("ffmpeg"), "-hide_banner", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=8",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=8",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", source,
        ])
        tokens = "A COMPLETE LOCAL PIPELINE WITH ANIMATED CAPTIONS WORKS".split()
        words = [Word(index * 0.7, (index + 1) * 0.7, token) for index, token in enumerate(tokens)]
        transcript = Transcript(8, "en", [Segment(0, 6.3, " ".join(tokens), words)])
        duration = render_clip(
            source, Highlight(0, 6.3, "Smoke test", "Local pipeline", "Validation", 80),
            transcript, output, root / "work", (360, 640), "9:16", True, True,
            "Impact", True, manager, print,
        )
        assert duration >= 6
        assert validate_mp4(output, manager) >= 6
        print(f"Smoke render passed: {output.stat().st_size} bytes, {duration:.2f}s")
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
