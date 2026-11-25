from __future__ import annotations

from math import ceil

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap, QResizeEvent
from PySide6.QtWidgets import (
    QFrame, QGridLayout, QHBoxLayout, QLabel, QProgressBar, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

from ..media import DownloadProgress


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
    image = QPixmap(16, 16)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor("#f4a1aa"), 1.6)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.drawLine(4, 4, 12, 12)
    painter.drawLine(12, 4, 4, 12)
    painter.end()
    return QIcon(image)


class DownloadView(QWidget):
    def __init__(self) -> None:
        super().__init__(objectName="downloadView")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 3)
        layout.setSpacing(6)

        header = QHBoxLayout()
        self.indicator = QFrame(objectName="transferIndicator")
        self.indicator.setFixedSize(7, 7)
        self.status = QLabel(objectName="transferStatus")
        self.status.setWordWrap(True)
        self.status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.percent = QLabel("—", objectName="transferPercent")
        self.percent.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.cancel_button = QPushButton(cancel_icon(), "Cancel Download", objectName="cancelDownload")
        header.addWidget(self.indicator)
        header.addWidget(self.status, 1)
        header.addWidget(self.percent)
        header.addWidget(self.cancel_button)
        layout.addLayout(header)

        self.bar = QProgressBar(objectName="downloadTransfer")
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(9)
        self.bar.setAccessibleName("Download progress")
        layout.addWidget(self.bar)
        self.summary = QLabel(objectName="transferSummary")
        layout.addWidget(self.summary)

        self.metrics = QGridLayout()
        self.metrics.setHorizontalSpacing(14)
        self.metrics.setVerticalSpacing(1)
        self.metric_labels = []
        self.metric_values = []
        for name in ("Transfer Rate", "Downloaded Size", "Total File Size", "Time Left"):
            label = QLabel(name, objectName="transferMetricLabel")
            value = QLabel(objectName="transferMetric")
            value.setAccessibleName(name)
            self.metric_labels.append(label)
            self.metric_values.append(value)
        layout.addLayout(self.metrics)
        self._columns = 0
        self._arrange_metrics(2)

        self.animation = QPropertyAnimation(self.bar, b"value", self)
        self.animation.setDuration(175)
        self.animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._stream = ""
        self.reset()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self.percent.setMinimumWidth(self.percent.fontMetrics().horizontalAdvance("100.0%") + 2)
        samples = ("1000.00 MB/s", "999.99 GB", "≈ 999.99 GB", "59 min 59 sec")
        widths = [
            max(label.sizeHint().width(), value.fontMetrics().horizontalAdvance(sample))
            for label, value, sample in zip(self.metric_labels, self.metric_values, samples)
        ]
        wide_width = sum(widths) + 3 * self.metrics.horizontalSpacing()
        columns = 4 if event.size().width() >= wide_width else 2
        self._arrange_metrics(columns)
        for index in range(4):
            self.metrics.setColumnMinimumWidth(index, max(widths[index::columns]) if index < columns else 0)

    def _arrange_metrics(self, columns: int) -> None:
        if self._columns == columns:
            return
        self._columns = columns
        for index, (label, value) in enumerate(zip(self.metric_labels, self.metric_values)):
            row, column = divmod(index, columns)
            self.metrics.addWidget(label, row * 2, column)
            self.metrics.addWidget(value, row * 2 + 1, column)
        for index in range(4):
            self.metrics.setColumnStretch(index, int(index < columns))

    def reset(self) -> None:
        self._stream = ""
        self.status.setText("Preparing download…")
        self.cancel_button.setEnabled(True)
        self.percent.setText("—")
        self.summary.clear()
        self.bar.setAccessibleDescription("Waiting for download progress")
        self.animation.stop()
        self.bar.setValue(0)
        for value in self.metric_values:
            value.setText("—")

    def update_transfer(self, progress: DownloadProgress) -> None:
        stream_changed = progress.stream != self._stream
        self._stream = progress.stream
        if progress.finished:
            self.status.setText(f"{progress.stream.title() or 'File'} downloaded")
        else:
            self.status.setText(f"Downloading {progress.stream or 'file'}")
        downloaded = format_bytes(progress.downloaded)
        total = format_bytes(progress.total)
        if progress.estimated:
            total = f"≈ {total}"
        if progress.downloaded is None:
            self.summary.clear()
        else:
            self.summary.setText(f"{downloaded} of {total}" if progress.total else f"{downloaded} downloaded")

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
        target = round(percent * 10) if percent is not None else 0
        self.animation.stop()
        if stream_changed or target <= self.bar.value() or not self.isVisible():
            self.bar.setValue(target)
        else:
            self.animation.setStartValue(self.bar.value())
            self.animation.setEndValue(target)
            self.animation.start()

        metrics = (format_bytes(progress.bytes_per_second, rate=True), downloaded, total,
                   format_time(0 if progress.finished else progress.eta_seconds))
        for label, value in zip(self.metric_values, metrics):
            label.setText(value)
