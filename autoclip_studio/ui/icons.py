"""Crisp Lucide-style 24x24 stroke icons rendered natively via QPainter."""
from __future__ import annotations

from functools import lru_cache

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap


@lru_cache(maxsize=256)
def lucide_icon(name: str, color: str = "#F5F5F7", size: int = 16) -> QIcon:
    """Render a Lucide-style vector icon on a transparent QPixmap at high-DPI."""
    dpr = 2.0
    px = int(round(size * dpr))
    pixmap = QPixmap(px, px)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.scale(px / 24.0, px / 24.0)

    pen = QPen(QColor(color), 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)

    if name == "play":
        path = QPainterPath()
        path.moveTo(6, 4)
        path.lineTo(19, 12)
        path.lineTo(6, 20)
        path.closeSubpath()
        painter.setBrush(QColor(color))
        painter.drawPath(path)
    elif name == "pause":
        painter.setBrush(QColor(color))
        painter.drawRoundedRect(QRectF(6, 4, 4, 16), 1, 1)
        painter.drawRoundedRect(QRectF(14, 4, 4, 16), 1, 1)
    elif name == "skip-back":
        painter.drawLine(QPointF(5, 5), QPointF(5, 19))
        path = QPainterPath()
        path.moveTo(19, 20)
        path.lineTo(9, 12)
        path.lineTo(19, 4)
        path.closeSubpath()
        painter.drawPath(path)
    elif name == "skip-forward":
        painter.drawLine(QPointF(19, 5), QPointF(19, 19))
        path = QPainterPath()
        path.moveTo(5, 4)
        path.lineTo(15, 12)
        path.lineTo(5, 20)
        path.closeSubpath()
        painter.drawPath(path)
    elif name in {"volume-2", "volume-x"}:
        path = QPainterPath()
        path.moveTo(11, 5)
        path.lineTo(6, 9)
        path.lineTo(2, 9)
        path.lineTo(2, 15)
        path.lineTo(6, 15)
        path.lineTo(11, 19)
        path.closeSubpath()
        painter.drawPath(path)
        if name == "volume-2":
            painter.drawArc(QRectF(12, 8.5, 5, 7), -55 * 16, 110 * 16)
            painter.drawArc(QRectF(14, 5.5, 7, 13), -55 * 16, 110 * 16)
        else:
            painter.drawLine(QPointF(16, 9), QPointF(22, 15))
            painter.drawLine(QPointF(22, 9), QPointF(16, 15))
    elif name == "maximize":
        for pts in (
            ((3, 8), (3, 3), (8, 3)),
            ((16, 3), (21, 3), (21, 8)),
            ((21, 16), (21, 21), (16, 21)),
            ((8, 21), (3, 21), (3, 16)),
        ):
            p = QPainterPath()
            p.moveTo(*pts[0]); p.lineTo(*pts[1]); p.lineTo(*pts[2])
            painter.drawPath(p)
    elif name == "minimize":
        for pts in (
            ((8, 3), (8, 8), (3, 8)),
            ((21, 8), (16, 8), (16, 3)),
            ((16, 21), (16, 16), (21, 16)),
            ((3, 16), (8, 16), (8, 21)),
        ):
            p = QPainterPath()
            p.moveTo(*pts[0]); p.lineTo(*pts[1]); p.lineTo(*pts[2])
            painter.drawPath(p)
    elif name == "list":
        for y in (6, 12, 18):
            painter.drawLine(QPointF(8, y), QPointF(21, y))
            painter.drawPoint(QPointF(3.5, y))
    elif name == "grid":
        for x in (3, 14):
            for y in (3, 14):
                painter.drawRoundedRect(QRectF(x, y, 7, 7), 1.5, 1.5)
    elif name == "search":
        painter.drawEllipse(QPointF(11, 11), 7, 7)
        painter.drawLine(QPointF(16.2, 16.2), QPointF(21, 21))
    elif name == "download":
        painter.drawLine(QPointF(12, 3), QPointF(12, 15))
        p = QPainterPath()
        p.moveTo(7, 10); p.lineTo(12, 15); p.lineTo(17, 10)
        painter.drawPath(p)
        p2 = QPainterPath()
        p2.moveTo(21, 15); p2.lineTo(21, 19); p2.lineTo(3, 19); p2.lineTo(3, 15)
        painter.drawPath(p2)
    elif name == "more-horizontal":
        painter.setBrush(QColor(color))
        for x in (5, 12, 19):
            painter.drawEllipse(QPointF(x, 12), 1.3, 1.3)
    elif name == "external-link":
        p = QPainterPath()
        p.moveTo(18, 13); p.lineTo(18, 19); p.lineTo(5, 19); p.lineTo(5, 6); p.lineTo(11, 6)
        painter.drawPath(p)
        painter.drawLine(QPointF(15, 3), QPointF(21, 3))
        painter.drawLine(QPointF(21, 3), QPointF(21, 9))
        painter.drawLine(QPointF(10, 14), QPointF(21, 3))
    elif name == "folder-open":
        p = QPainterPath()
        p.moveTo(3, 7); p.lineTo(3, 19); p.lineTo(20, 19); p.lineTo(22, 11)
        p.lineTo(7, 11); p.lineTo(5, 19)
        painter.drawPath(p)
        p2 = QPainterPath()
        p2.moveTo(3, 7); p2.lineTo(3, 5); p2.lineTo(9, 5); p2.lineTo(11, 7); p2.lineTo(19, 7); p2.lineTo(19, 11)
        painter.drawPath(p2)
    elif name == "refresh-cw":
        painter.drawArc(QRectF(4, 4, 16, 16), 35 * 16, 290 * 16)
        p = QPainterPath()
        p.moveTo(17, 3); p.lineTo(21, 7); p.lineTo(16, 9)
        painter.drawPath(p)
    elif name == "image":
        painter.drawRoundedRect(QRectF(3, 3, 18, 18), 2, 2)
        painter.drawEllipse(QPointF(8.5, 8.5), 1.5, 1.5)
        p = QPainterPath()
        p.moveTo(21, 15); p.lineTo(16, 10); p.lineTo(5, 21)
        painter.drawPath(p)
    elif name == "edit-3":
        painter.drawLine(QPointF(12, 20), QPointF(21, 20))
        p = QPainterPath()
        p.moveTo(16.5, 3.5); p.lineTo(20.5, 7.5); p.lineTo(7, 21); p.lineTo(3, 21); p.lineTo(3, 17)
        p.closeSubpath()
        painter.drawPath(p)
    elif name == "trash-2":
        painter.drawLine(QPointF(3, 6), QPointF(21, 6))
        painter.drawLine(QPointF(8, 6), QPointF(8, 4))
        painter.drawLine(QPointF(16, 6), QPointF(16, 4))
        painter.drawLine(QPointF(8, 4), QPointF(16, 4))
        p = QPainterPath()
        p.moveTo(5, 6); p.lineTo(6, 20); p.lineTo(18, 20); p.lineTo(19, 6)
        painter.drawPath(p)
        painter.drawLine(QPointF(10, 11), QPointF(10, 16))
        painter.drawLine(QPointF(14, 11), QPointF(14, 16))
    elif name == "chevron-down":
        p = QPainterPath()
        p.moveTo(6, 9); p.lineTo(12, 15); p.lineTo(18, 9)
        painter.drawPath(p)
    elif name == "chevron-left":
        p = QPainterPath()
        p.moveTo(15, 6); p.lineTo(9, 12); p.lineTo(15, 18)
        painter.drawPath(p)
    elif name == "chevron-right":
        p = QPainterPath()
        p.moveTo(9, 6); p.lineTo(15, 12); p.lineTo(9, 18)
        painter.drawPath(p)
    elif name == "arrow-up":
        painter.drawLine(QPointF(12, 19), QPointF(12, 5))
        p = QPainterPath()
        p.moveTo(6, 11); p.lineTo(12, 5); p.lineTo(18, 11)
        painter.drawPath(p)
    elif name == "arrow-down":
        painter.drawLine(QPointF(12, 5), QPointF(12, 19))
        p = QPainterPath()
        p.moveTo(6, 13); p.lineTo(12, 19); p.lineTo(18, 13)
        painter.drawPath(p)
    elif name == "film":
        painter.drawRoundedRect(QRectF(3, 4, 18, 16), 2, 2)
        painter.drawLine(QPointF(7, 4), QPointF(7, 20))
        painter.drawLine(QPointF(17, 4), QPointF(17, 20))
        painter.drawLine(QPointF(3, 9), QPointF(7, 9))
        painter.drawLine(QPointF(3, 15), QPointF(7, 15))
        painter.drawLine(QPointF(17, 9), QPointF(21, 9))
        painter.drawLine(QPointF(17, 15), QPointF(21, 15))
    elif name == "globe":
        painter.drawEllipse(QPointF(12, 12), 9, 9)
        painter.drawEllipse(QPointF(12, 12), 4, 9)
        painter.drawLine(QPointF(3, 12), QPointF(21, 12))
    elif name == "x":
        painter.drawLine(QPointF(6, 6), QPointF(18, 18))
        painter.drawLine(QPointF(18, 6), QPointF(6, 18))
    elif name == "alert-circle":
        painter.drawEllipse(QPointF(12, 12), 9, 9)
        painter.drawLine(QPointF(12, 8), QPointF(12, 13))
        painter.drawPoint(QPointF(12, 16.5))
    elif name == "check":
        p = QPainterPath()
        p.moveTo(5, 13); p.lineTo(9.5, 17.5); p.lineTo(19, 7)
        painter.drawPath(p)
    elif name == "clock":
        painter.drawEllipse(QPointF(12, 12), 9, 9)
        p = QPainterPath()
        p.moveTo(12, 7); p.lineTo(12, 12); p.lineTo(15.5, 14)
        painter.drawPath(p)
    elif name == "sun":
        painter.drawEllipse(QPointF(12, 12), 4, 4)
        for p1, p2 in (
            ((12, 2), (12, 4)), ((12, 20), (12, 22)),
            ((2, 12), (4, 12)), ((20, 12), (22, 12)),
            ((4.9, 4.9), (6.3, 6.3)), ((17.7, 17.7), (19.1, 19.1)),
            ((4.9, 19.1), (6.3, 17.7)), ((17.7, 6.3), (19.1, 4.9)),
        ):
            painter.drawLine(QPointF(*p1), QPointF(*p2))
    elif name == "moon":
        p = QPainterPath()
        p.moveTo(20, 14.5)
        p.cubicTo(18.6, 15.1, 17.1, 15.4, 15.5, 15.4)
        p.cubicTo(11.1, 15.4, 7.6, 11.9, 7.6, 7.5)
        p.cubicTo(7.6, 6.0, 8.0, 4.6, 8.7, 3.4)
        p.cubicTo(5.3, 4.8, 3.0, 8.1, 3.0, 12.0)
        p.cubicTo(3.0, 17.0, 7.0, 21.0, 12.0, 21.0)
        p.cubicTo(15.7, 21.0, 18.8, 18.7, 20.0, 14.5)
        p.closeSubpath()
        painter.drawPath(p)
    elif name == "monitor":
        painter.drawRoundedRect(QRectF(2.5, 3.5, 19, 13), 2, 2)
        painter.drawLine(QPointF(8, 20.5), QPointF(16, 20.5))
        painter.drawLine(QPointF(12, 16.5), QPointF(12, 20.5))
    elif name == "chevron-up":
        p = QPainterPath()
        p.moveTo(6, 15); p.lineTo(12, 9); p.lineTo(18, 15)
        painter.drawPath(p)

    painter.end()
    pixmap.setDevicePixelRatio(dpr)
    return QIcon(pixmap)
