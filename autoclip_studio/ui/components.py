"""Centralized shared UI components for AutoClip Studio.

Provides theme-aware, consistently sized widgets adhering to the 4px spacing scale
and unified 36px control height across macOS (Apple Silicon) and Windows:
- Button factory (`make_button`, `sync_button_icon`) with 5 semantic variants
- Compact semantic `StatusBadge` (`pending`, `processing`, `queued`, `completed`, `failed`, `cancelled`, `ready`)
- Reusable `MetricGrid` and `StageProgressWidget` for downloads, transcription, export, and dependencies
"""
from __future__ import annotations

from typing import Literal

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QResizeEvent
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .icons import lucide_icon
from .theme import SPACING, theme_manager

ButtonVariant = Literal["primary", "secondary", "ghost", "destructive", "subtle"]
BadgeStatus = Literal[
    "neutral", "pending", "processing", "queued", "completed", "failed", "cancelled", "ready", "info", "warning", "danger", "success"
]

CONTROL_HEIGHT = 36

_VARIANT_OBJECT_NAME: dict[str, str] = {
    "primary": "primary",
    "secondary": "",
    "ghost": "ghost",
    "destructive": "danger",
    "subtle": "subtle",
}

_STATUS_TO_TONE: dict[str, str] = {
    "neutral": "",
    "pending": "warning",
    "processing": "info",
    "queued": "warning",
    "completed": "success",
    "ready": "success",
    "success": "success",
    "failed": "danger",
    "analysis_failed": "danger",
    "danger": "danger",
    "cancelled": "",
    "info": "info",
    "warning": "warning",
}


def button_icon_color(variant: str) -> str:
    t = theme_manager().tokens
    if variant == "primary":
        return t["text_on_brand"]
    if variant == "destructive":
        return t["semantic_danger_fg"]
    if variant in ("ghost", "subtle"):
        return t["text_secondary"]
    return t["text_primary"]


def sync_button_icon(button: QPushButton, icon_name: str, variant: str = "secondary", size: int = 15) -> None:
    """Update a button's Lucide icon color cleanly for the active theme."""
    button.setIcon(lucide_icon(icon_name, button_icon_color(variant), size))
    button.setIconSize(QSize(size, size))


def make_button(
    text: str = "",
    *,
    variant: ButtonVariant = "secondary",
    icon_name: str | None = None,
    icon_size: int = 15,
    tooltip: str = "",
    accessible_name: str = "",
    parent: QWidget | None = None,
) -> QPushButton:
    """Create a standardized button following global height, padding, icon alignment, and theme tokens."""
    btn = QPushButton(text, parent)
    obj_name = _VARIANT_OBJECT_NAME.get(variant, "")
    if obj_name:
        btn.setObjectName(obj_name)
    btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
    if icon_name:
        sync_button_icon(btn, icon_name, variant, icon_size)
    if tooltip:
        btn.setToolTip(tooltip)
    if accessible_name:
        btn.setAccessibleName(accessible_name)
    return btn


class StatusBadge(QLabel):
    """Compact informational status badge (never styled as an oversized action button)."""

    def __init__(
        self,
        text: str = "",
        *,
        status: BadgeStatus = "neutral",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(text, parent)
        self.setObjectName("statusChip")
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status = status
        self.set_status(text, status)

    def set_status(self, text: str, status: str = "neutral") -> None:
        self._status = status
        self.setText(text)
        tone = _STATUS_TO_TONE.get(status, "")
        self.setProperty("tone", tone)
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()


class MetricGrid(QWidget):
    """Responsive, collision-free label/value metric grid for download, speech, export, and dependency progress."""

    def __init__(
        self,
        specs: tuple[tuple[str, str], ...],
        *,
        full_width_keys: tuple[str, ...] = (),
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._keys: list[str] = [k for k, _ in specs]
        self._full_width: set[str] = set(full_width_keys)
        self._regular_keys: list[str] = [k for k in self._keys if k not in self._full_width]
        self._full_keys: list[str] = [k for k in self._keys if k in self._full_width]
        self._columns: int = 0

        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 2, 0, 0)
        self.grid.setHorizontalSpacing(SPACING["ms"])
        self.grid.setVerticalSpacing(SPACING["xs"] + 2)

        self.labels: dict[str, QLabel] = {}
        self.values: dict[str, QLabel] = {}
        self._cells: dict[str, QWidget] = {}

        for key, display_label in specs:
            cell = QWidget(self)
            cell.setObjectName("metricCell")
            cell.setMinimumWidth(0)
            cell.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            cl = QVBoxLayout(cell)
            cl.setContentsMargins(0, 0, 0, 0)
            cl.setSpacing(1)

            lbl = QLabel(display_label, cell)
            lbl.setObjectName("transferMetricLabel")
            lbl.setMinimumWidth(0)

            val = QLabel("—", cell)
            val.setObjectName("transferMetric")
            val.setAccessibleName(display_label)
            val.setMinimumWidth(0)
            val.setWordWrap(key in self._full_width)

            cl.addWidget(lbl)
            cl.addWidget(val)
            self.labels[key] = lbl
            self.values[key] = val
            self._cells[key] = cell

        self._arrange(min(4, max(2, len(self._regular_keys))))

    def set_label(self, key: str, text: str) -> None:
        if key in self.labels:
            self.labels[key].setText(text)
            if key in self.values:
                self.values[key].setAccessibleName(text)

    def set_value(self, key: str, text: str) -> None:
        if key in self.values:
            self.values[key].setText(text or "—")

    def reset(self) -> None:
        for val in self.values.values():
            val.setText("—")

    def _arrange(self, columns: int) -> None:
        columns = max(1, min(4, columns))
        if self._columns == columns:
            return
        self._columns = columns
        while self.grid.count():
            self.grid.takeAt(0)
        for idx, key in enumerate(self._regular_keys):
            row, col = divmod(idx, columns)
            self.grid.addWidget(self._cells[key], row, col)
        next_row = (len(self._regular_keys) + columns - 1) // columns
        for offset, key in enumerate(self._full_keys):
            self.grid.addWidget(self._cells[key], next_row + offset, 0, 1, columns)
        for c in range(4):
            self.grid.setColumnStretch(c, 1 if c < columns else 0)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        w = event.size().width()
        n = len(self._regular_keys)
        if n >= 4:
            cols = 4 if w >= 500 else 2
        elif n == 3:
            cols = 3 if w >= 420 else 2
        else:
            cols = max(1, min(2, n))
        self._arrange(cols)


class StageProgressHeader(QWidget):
    """Shared stage progress header: indicator dot, wrapping title, percentage, subtitle, and progress bar."""

    def __init__(
        self,
        initial_title: str = "Preparing…",
        *,
        accessible_bar_name: str = "Progress",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(SPACING["xs"] + 2)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(SPACING["sm"])

        self.indicator = QFrame(self)
        self.indicator.setObjectName("transferIndicator")
        self.indicator.setFixedSize(8, 8)

        self.title_label = QLabel(initial_title, self)
        self.title_label.setObjectName("transferStatus")
        self.title_label.setWordWrap(True)
        self.title_label.setMinimumWidth(0)
        self.title_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        self.percent_label = QLabel("—", self)
        self.percent_label.setObjectName("transferPercentCompact")
        self.percent_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.percent_label.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

        top.addWidget(self.indicator, 0, Qt.AlignmentFlag.AlignVCenter)
        top.addWidget(self.title_label, 1)
        top.addWidget(self.percent_label, 0, Qt.AlignmentFlag.AlignVCenter)
        root.addLayout(top)

        self.subtitle_label = QLabel("", self)
        self.subtitle_label.setObjectName("transferSummary")
        self.subtitle_label.setWordWrap(True)
        self.subtitle_label.setTextFormat(Qt.TextFormat.PlainText)
        self.subtitle_label.setMinimumWidth(0)
        self.subtitle_label.hide()
        root.addWidget(self.subtitle_label)

        self.bar = QProgressBar(self)
        self.bar.setObjectName("downloadTransfer")
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(8)
        self.bar.setAccessibleName(accessible_bar_name)
        root.addWidget(self.bar)

    def set_subtitle(self, text: str) -> None:
        cleaned = (text or "").strip()
        self.subtitle_label.setText(cleaned)
        self.subtitle_label.setVisible(bool(cleaned))

    def set_indeterminate(self, indeterminate: bool) -> None:
        if indeterminate:
            if self.bar.maximum() != 0:
                self.bar.setRange(0, 0)
        elif self.bar.maximum() != 1000:
            self.bar.setRange(0, 1000)
