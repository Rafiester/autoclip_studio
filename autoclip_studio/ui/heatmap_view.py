"""Compact interactive YouTube Audience Replay Heatmap widget for the New Project page."""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QLinearGradient, QMouseEvent, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from ..heatmap import HeatmapPoint, HighlightRegion, format_clock, parse_heatmap, summarize_heatmap
from .components import StatusBadge
from .theme import SPACING, theme_manager


class _HeatmapGraph(QWidget):
    """Interactive red-and-black replay intensity curve with highlighted peak regions and hover inspector."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(88)
        self.setMaximumHeight(112)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMouseTracking(True)
        self._points: list[HeatmapPoint] = []
        self._regions: list[HighlightRegion] = []
        self._hover_idx: int = -1
        self._duration: float = 0.0

    def set_data(self, points: list[HeatmapPoint], regions: list[HighlightRegion], duration: float = 0.0) -> None:
        self._points = points
        self._regions = regions
        self._duration = max(duration, points[-1].end_time if points else 0.0)
        self._hover_idx = -1
        self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if not self._points or self.width() <= 2:
            return
        ratio = max(0.0, min(1.0, event.position().x() / max(1.0, float(self.width()))))
        idx = min(len(self._points) - 1, int(ratio * len(self._points)))
        if idx != self._hover_idx:
            self._hover_idx = idx
            pt = self._points[idx]
            self.setToolTip(
                f"{format_clock(pt.start_time)} – {format_clock(pt.end_time)}\n"
                f"Relative replay intensity: {round(pt.value * 100)}%"
            )
            self.update()

    def leaveEvent(self, event) -> None:
        if self._hover_idx != -1:
            self._hover_idx = -1
            self.update()
        super().leaveEvent(event)

    def paintEvent(self, event) -> None:
        t = theme_manager().tokens
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        bounds = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        clip = QPainterPath()
        clip.addRoundedRect(bounds, 8, 8)
        painter.setClipPath(clip)
        painter.fillRect(bounds, QColor(t["bg_elevated"]))

        if not self._points:
            painter.setPen(QColor(t["text_secondary"]))
            painter.drawText(bounds, Qt.AlignmentFlag.AlignCenter, "No audience replay heatmap available for this video")
            return

        pad_x = 6.0
        pad_top = 10.0
        pad_bottom = 10.0
        plot_w = max(1.0, bounds.width() - pad_x * 2)
        plot_h = max(1.0, bounds.height() - pad_top - pad_bottom)
        total = max(1.0, self._duration)

        # Draw subtle highlighted region bands
        for region in self._regions:
            rx0 = pad_x + (region.start / total) * plot_w
            rx1 = pad_x + (region.end / total) * plot_w
            painter.fillRect(QRectF(rx0, pad_top, max(2.0, rx1 - rx0), plot_h), QColor(229, 57, 53, 42))

        # Build curve path
        coords: list[QPointF] = []
        for pt in self._points:
            x = pad_x + (pt.midpoint / total) * plot_w
            y = pad_top + (1.0 - pt.value) * plot_h
            coords.append(QPointF(x, y))

        if coords:
            line = QPainterPath(coords[0])
            for c in coords[1:]:
                line.lineTo(c)
            area = QPainterPath(line)
            area.lineTo(QPointF(coords[-1].x(), pad_top + plot_h))
            area.lineTo(QPointF(coords[0].x(), pad_top + plot_h))
            area.closeSubpath()

            grad = QLinearGradient(0, pad_top, 0, pad_top + plot_h)
            grad.setColorAt(0.0, QColor(229, 57, 53, 120))
            grad.setColorAt(1.0, QColor(229, 57, 53, 12))
            painter.fillPath(area, grad)
            painter.setPen(QPen(QColor("#FF5A4F"), 1.8))
            painter.drawPath(line)

        # Draw peak markers
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#FFD45A"))
        for region in self._regions[:5]:
            px = pad_x + (region.peak_time / total) * plot_w
            py = pad_top + (1.0 - region.peak_intensity) * plot_h
            painter.drawEllipse(QPointF(px, py), 3.2, 3.2)

        # Draw hover crosshair
        if 0 <= self._hover_idx < len(coords):
            hc = coords[self._hover_idx]
            painter.setPen(QPen(QColor(t["text_primary"]), 1.0, Qt.PenStyle.DashLine))
            painter.drawLine(QPointF(hc.x(), pad_top), QPointF(hc.x(), pad_top + plot_h))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(t["text_primary"]))
            painter.drawEllipse(hc, 3.8, 3.8)


class AudienceHeatmapWidget(QFrame):
    """Compact Audience Heatmap summary card shown beneath YouTubePreview."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent, objectName="summaryPill")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(SPACING["md"], SPACING["ms"], SPACING["md"], SPACING["ms"])
        layout.setSpacing(SPACING["sm"])

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(SPACING["sm"])
        self.title_label = QLabel("Audience Replay Heatmap", objectName="cardTitle")
        self.availability_chip = StatusBadge("Unavailable", status="neutral", parent=self)
        self.mode_chip = StatusBadge("Mode: AI Analysis", status="neutral", parent=self)
        header.addWidget(self.title_label)
        header.addWidget(self.availability_chip)
        header.addStretch()
        header.addWidget(self.mode_chip)
        layout.addLayout(header)

        self.graph = _HeatmapGraph()
        layout.addWidget(self.graph)

        self.peaks_label = QLabel("", objectName="muted")
        self.peaks_label.setWordWrap(True)
        self.peaks_label.setMinimumWidth(0)
        layout.addWidget(self.peaks_label)

        self.regions_label = QLabel("", objectName="muted")
        self.regions_label.setWordWrap(True)
        self.regions_label.setMinimumWidth(0)
        layout.addWidget(self.regions_label)

        self.hide()

    def set_mode_label(self, mode_text: str) -> None:
        self.mode_chip.setText(f"Mode: {mode_text}")

    def clear(self) -> None:
        self.graph.set_data([], [], 0.0)
        self.availability_chip.setText("Unavailable")
        self.peaks_label.setText("")
        self.regions_label.setText("")
        self.hide()

    def apply_theme(self, _resolved: str = "") -> None:
        self.graph.update()

    def set_heatmap(
        self,
        raw_heatmap: object,
        *,
        duration: float | None = None,
        min_duration: float = 25.0,
        max_duration: float = 60.0,
        mode_text: str = "Hybrid",
    ) -> bool:
        points = parse_heatmap(raw_heatmap, duration=duration)
        summary = summarize_heatmap(
            points,
            min_duration=min_duration,
            max_duration=max_duration,
            video_duration=duration,
        )
        self.set_mode_label(mode_text)
        if not summary.available:
            self.availability_chip.set_status("Heatmap Unavailable", "neutral")
            self.graph.set_data([], [], float(duration or 0.0))
            self.peaks_label.setText("YouTube did not provide replay heatmap data for this video. Falling back to AI Analysis.")
            self.regions_label.setText("")
            self.show()
            return False

        self.availability_chip.set_status(f"Heatmap Available ({len(summary.points)} pts)", "ready")
        self.graph.set_data(list(summary.points), list(summary.regions), float(duration or 0.0))
        if summary.peak_timestamps:
            peaks_str = ", ".join(format_clock(ts) for ts in summary.peak_timestamps[:5])
            self.peaks_label.setText(f"Most replayed timestamps: {peaks_str}")
        else:
            self.peaks_label.setText("Relative replay curve detected (no isolated sharp spikes).")
        if summary.regions:
            reg_str = "  •  ".join(
                f"{format_clock(r.start)}–{format_clock(r.end)} ({r.replay_score}/100)"
                for r in summary.regions[:4]
            )
            self.regions_label.setText(f"Detected highlight regions: {reg_str}")
        else:
            self.regions_label.setText("")
        self.show()
        return True
