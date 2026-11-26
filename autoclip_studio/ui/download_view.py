from __future__ import annotations

from math import ceil

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QVBoxLayout, QWidget

from ..media import DownloadProgress
from .components import MetricGrid, StageProgressHeader, make_button, sync_button_icon
from .icons import lucide_icon
from .theme import SPACING, theme_manager


def format_bytes(value: int | float | None, *, rate: bool = False) -> str:
    if value is None:
        return "—"
    units = ("KB", "MB") if rate else ("KB", "MB", "GB")
    amount = value / 1000
    for unit in units[:-1]:
        if amount < 1000:
            break
        amount /= 1000
    else:
        unit = units[-1]
    return f"{amount:.2f} {unit}{'/s' if rate else ''}"


def format_time(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    minutes, seconds = divmod(ceil(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days} d {hours} hr"
    if hours:
        return f"{hours} hr {minutes} min"
    if minutes:
        return f"{minutes} min {seconds} sec"
    return f"{seconds} sec"


def cancel_icon() -> QIcon:
    return lucide_icon("x", theme_manager().tokens["semantic_danger_fg"], 14)


class DownloadView(QWidget):
    """Shared transfer progress and metrics panel built on StageProgressHeader + MetricGrid."""

    def __init__(self, *, embedded: bool = False) -> None:
        super().__init__(objectName="downloadView")
        self._embedded = embedded
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(SPACING["sm"])

        top_row = QHBoxLayout()
        top_row.setContentsMargins(0, 0, 0, 0)
        top_row.setSpacing(SPACING["sm"])

        self.progress_header = StageProgressHeader(
            "Preparing download…",
            accessible_bar_name="Download progress",
            parent=self,
        )
        self.indicator = self.progress_header.indicator
        self.status = self.progress_header.title_label
        self.percent = self.progress_header.percent_label
        self.summary = self.progress_header.subtitle_label
        self.bar = self.progress_header.bar

        self.cancel_button = make_button(
            "Cancel Download",
            variant="destructive",
            icon_name="x",
            icon_size=14,
            parent=self,
        )
        self.cancel_button.setObjectName("cancelDownload")
        top_row.addWidget(self.progress_header, 1)
        top_row.addWidget(self.cancel_button, 0, Qt.AlignmentFlag.AlignTop)
        if self._embedded:
            self.cancel_button.hide()
        layout.addLayout(top_row)

        self.metrics_grid = MetricGrid(
            (
                ("rate", "Transfer Rate"),
                ("downloaded", "Downloaded Size"),
                ("total", "Total File Size"),
                ("eta", "Time Left"),
            ),
            parent=self,
        )
        self.metrics = self.metrics_grid.grid
        self.metric_labels = [self.metrics_grid.labels[k] for k in ("rate", "downloaded", "total", "eta")]
        self.metric_values = [self.metrics_grid.values[k] for k in ("rate", "downloaded", "total", "eta")]
        layout.addWidget(self.metrics_grid)

        self.animation = QPropertyAnimation(self.bar, b"value", self)
        self.animation.setDuration(175)
        self.animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._stream = ""
        self.reset()

    def apply_theme(self, _resolved: str = "") -> None:
        sync_button_icon(self.cancel_button, "x", "destructive", 14)

    def reset(self) -> None:
        self._stream = ""
        self.status.setText("Preparing download…")
        self.cancel_button.setEnabled(True)
        self.percent.setText("—")
        self.progress_header.set_subtitle("")
        self.progress_header.set_indeterminate(False)
        self.bar.setAccessibleDescription("Waiting for download progress")
        self.animation.stop()
        self.bar.setValue(0)
        self.metrics_grid.reset()

    def update_transfer(self, progress: DownloadProgress) -> None:
        stream_changed = progress.stream != self._stream
        self._stream = progress.stream
        if progress.finished:
            self.status.setText(f"{progress.stream.title() or 'File'} downloaded")
        else:
            self.status.setText(f"Downloading {progress.stream or 'file'}")

        downloaded = format_bytes(progress.downloaded)
        total = format_bytes(progress.total)
        if progress.estimated and progress.total is not None:
            total = f"≈ {total}"
        if progress.downloaded is None:
            self.progress_header.set_subtitle("")
        else:
            self.progress_header.set_subtitle(
                f"{downloaded} of {total}" if progress.total else f"{downloaded} downloaded"
            )

        percent = None
        if progress.finished:
            percent = 100.0
        elif progress.total and progress.downloaded is not None:
            ceiling = 99.9 if progress.estimated else 100
            percent = min(ceiling, 100 * progress.downloaded / progress.total)
        self.percent.setText(f"{percent:.1f}%" if percent is not None else "—")
        description = "Total size is unknown" if percent is None else f"{self.percent.text()} downloaded"
        if progress.estimated:
            description += " (estimated)"
        self.bar.setAccessibleDescription(description)

        self.progress_header.set_indeterminate(False)
        target = round(percent * 10) if percent is not None else 0
        self.animation.stop()
        if stream_changed or target <= self.bar.value() or not self.isVisible():
            self.bar.setValue(target)
        else:
            self.animation.setStartValue(self.bar.value())
            self.animation.setEndValue(target)
            self.animation.start()

        self.metrics_grid.set_value("rate", format_bytes(progress.bytes_per_second, rate=True))
        self.metrics_grid.set_value("downloaded", downloaded)
        self.metrics_grid.set_value("total", total)
        self.metrics_grid.set_value("eta", format_time(0 if progress.finished else progress.eta_seconds))
