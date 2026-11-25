"""Generate the multi-resolution Windows icon from the AutoClip mark."""
import struct
import sys
from pathlib import Path

from PySide6.QtCore import QByteArray, QBuffer, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QLinearGradient, QPainter, QPen, QPolygonF


def icon_image(size: int) -> bytes:
    canvas = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    canvas.fill(Qt.GlobalColor.transparent)
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.scale(size / 128, size / 128)
    painter.setPen(Qt.PenStyle.NoPen)
    background = QLinearGradient(0, 0, 128, 128)
    background.setColorAt(0, QColor("#322184"))
    background.setColorAt(1, QColor("#17132f"))
    painter.setBrush(background)
    painter.drawRoundedRect(QRectF(3, 3, 122, 122), 27, 27)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    pen = QPen(QColor("#a89aff"), 7)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.drawArc(QRectF(27, 19, 74, 90), 41 * 16, 278 * 16)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#ffffff"))
    painter.drawPolygon(QPolygonF([QPointF(54, 43), QPointF(85, 64), QPointF(54, 85)]))
    painter.setBrush(QColor("#73f3df"))
    painter.drawEllipse(QPointF(100, 64), 6, 6)
    painter.end()
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QBuffer.OpenModeFlag.WriteOnly)
    if not canvas.save(buffer, "PNG"):
        raise RuntimeError("Could not encode icon image")
    return bytes(data)


def main(target: Path) -> None:
    sizes = (16, 24, 32, 48, 64, 128, 256)
    images = [icon_image(size) for size in sizes]
    index = bytearray(struct.pack("<HHH", 0, 1, len(sizes)))
    offset = 6 + 16 * len(sizes)
    for size, pixels in zip(sizes, images):
        index.extend(struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(pixels), offset))
        offset += len(pixels)
    target.parent.mkdir(parents=True, exist_ok=True)
    icon = bytes(index) + b"".join(images)
    if not target.exists() or target.read_bytes() != icon:
        target.write_bytes(icon)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
