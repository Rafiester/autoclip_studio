"""Generate a multi-resolution macOS .icns bundle icon from AutoClipJpg.jpg."""
from __future__ import annotations

import struct
import sys
from pathlib import Path

from PySide6.QtCore import QByteArray, QBuffer, Qt
from PySide6.QtGui import QImage

ROOT = Path(__file__).resolve().parent.parent
PACKAGING = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGING))

from make_icon import _load_symbol_source, render_symbol_image

# Standard PNG-backed Apple ICNS icon types (macOS 10.7+ / Apple Silicon)
ICNS_ENTRIES: tuple[tuple[bytes, int], ...] = (
    (b"icp4", 16),
    (b"icp5", 32),
    (b"icp6", 64),
    (b"ic07", 128),
    (b"ic08", 256),
    (b"ic09", 512),
    (b"ic10", 1024),
)


def _png_bytes(image: QImage) -> bytes:
    buf = QByteArray()
    qbuf = QBuffer(buf)
    qbuf.open(QBuffer.OpenModeFlag.WriteOnly)
    if not image.save(qbuf, "PNG"):
        raise RuntimeError("Failed to encode PNG chunk for ICNS")
    return bytes(buf)


def build_icns(target: Path, source_path: Path | None = None) -> Path:
    source = _load_symbol_source(source_path)
    chunks = bytearray()
    for ostype, size in ICNS_ENTRIES:
        png = _png_bytes(render_symbol_image(size, source))
        chunks.extend(ostype)
        chunks.extend(struct.pack(">I", 8 + len(png)))
        chunks.extend(png)
    header = b"icns" + struct.pack(">I", 8 + len(chunks))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(header + bytes(chunks))
    return target


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "autoclip_studio" / "assets" / "autoclip.icns"
    src = Path(sys.argv[2]) if len(sys.argv) > 2 else None
    build_icns(out, src)
