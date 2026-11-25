"""Generate the multi-resolution Windows icon and transparent symbol from the AutoClip Studio asset."""
from __future__ import annotations

import struct
import sys
from pathlib import Path

from PySide6.QtCore import QByteArray, QBuffer, QRectF, Qt
from PySide6.QtGui import QImage, QPainter, QPainterPath

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SYMBOL_SOURCE = ROOT / "autoclip_studio" / "assets" / "AutoClipJpg.jpg"
ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)


def _load_symbol_source(source: Path | None = None) -> QImage:
    candidate = source or DEFAULT_SYMBOL_SOURCE
    if not candidate.is_file():
        candidate = ROOT / "AutoClipJpg.jpg"
    image = QImage(str(candidate))
    if image.isNull():
        raise FileNotFoundError(f"Could not load brand symbol image: {candidate}")
    return image


def render_symbol_image(size: int, source: QImage) -> QImage:
    """Render the rounded-square symbol at `size`x`size` with transparent outer corners."""
    canvas = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    canvas.fill(Qt.GlobalColor.transparent)
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

    # Match the ~365/2048 corner radius of AutoClipJpg.jpg so white outer corners become transparent.
    radius = size * (365.0 / 2048.0)
    clip = QPainterPath()
    clip.addRoundedRect(QRectF(0.0, 0.0, float(size), float(size)), radius, radius)
    painter.setClipPath(clip)
    painter.drawImage(
        QRectF(0.0, 0.0, float(size), float(size)),
        source,
        QRectF(0.0, 0.0, float(source.width()), float(source.height())),
    )
    painter.end()
    return canvas


def icon_png_bytes(size: int, source: QImage) -> bytes:
    canvas = render_symbol_image(size, source)
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QBuffer.OpenModeFlag.WriteOnly)
    if not canvas.save(buffer, "PNG"):
        raise RuntimeError("Could not encode icon image")
    return bytes(data)


def main(target: Path, source_path: Path | None = None) -> None:
    source = _load_symbol_source(source_path)
    images = [icon_png_bytes(size, source) for size in ICON_SIZES]
    index = bytearray(struct.pack("<HHH", 0, 1, len(ICON_SIZES)))
    offset = 6 + 16 * len(ICON_SIZES)
    for size, pixels in zip(ICON_SIZES, images):
        index.extend(struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(pixels), offset))
        offset += len(pixels)
    target.parent.mkdir(parents=True, exist_ok=True)
    icon = bytes(index) + b"".join(images)
    if not target.exists() or target.read_bytes() != icon:
        target.write_bytes(icon)

    symbol_png = target.parent / "symbol.png"
    symbol_256 = render_symbol_image(256, source)
    symbol_256.save(str(symbol_png), "PNG")


if __name__ == "__main__":
    src = Path(sys.argv[2]) if len(sys.argv) > 2 else None
    main(Path(sys.argv[1]), src)
