from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QImage, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QFileDialog, QFrame, QHBoxLayout, QLabel,
    QPushButton, QSlider, QVBoxLayout, QWidget,
)

from ..thumbnails import (
    ThumbnailResult, export_thumbnail, extract_frame_at,
    generate_clip_thumbnail, max_thumbnail_workers,
)


def _format_duration(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


class ClipThumbnailPreview(QWidget):
    """Vertical 9:16-friendly thumbnail preview with loading skeleton and duration badge."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(180, 235)
        self._image = QImage()
        self._duration_text = ""
        self._loading = False
        self._status = ""

    def set_thumbnail(self, image: QImage | None, duration: float = 0.0, *,
                      loading: bool = False, status: str = "") -> None:
        self._image = image if image is not None else QImage()
        self._duration_text = _format_duration(duration) if duration > 0 else ""
        self._loading = loading
        self._status = status
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        bounds = QRectF(self.rect())
        clip = QPainterPath()
        clip.addRoundedRect(bounds, 10, 10)
        painter.setClipPath(clip)
        painter.fillRect(bounds, QColor("#22252E" if self._loading else "#101116"))

        if not self._image.isNull():
            scaled = self._image.size().scaled(
                QSize(int(bounds.width()), int(bounds.height())),
                Qt.AspectRatioMode.KeepAspectRatio,
            )
            target = QRectF(
                (bounds.width() - scaled.width()) / 2.0,
                (bounds.height() - scaled.height()) / 2.0,
                scaled.width(),
                scaled.height(),
            )
            painter.drawImage(target, self._image)
        else:
            painter.setPen(QColor("#A3A8B5"))
            label = "Generating thumbnail…" if self._loading else (self._status or "Select a clip")
            painter.drawText(bounds, Qt.AlignmentFlag.AlignCenter, label)

        if self._duration_text:
            font = QFont("Consolas")
            font.setPixelSize(12)
            font.setBold(True)
            painter.setFont(font)
            width = painter.fontMetrics().horizontalAdvance(self._duration_text) + 12
            badge = QRectF(bounds.width() - width - 8, bounds.height() - 28, width, 20)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(16, 17, 22, 224))
            painter.drawRoundedRect(badge, 4, 4)
            painter.setPen(QColor("#F5F5F7"))
            painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, self._duration_text)


class FramePickerDialog(QDialog):
    """Manual timeline frame selector for choosing a clip's thumbnail."""

    def __init__(self, clip_path: Path, duration: float, initial_ts: float | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.clip_path = clip_path
        self.duration = max(0.1, duration)
        self.selected_timestamp = initial_ts if initial_ts is not None else self.duration * 0.5
        self.setWindowTitle("Select Thumbnail Frame")
        self.resize(420, 600)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)
        layout.addWidget(QLabel("Scrub the clip timeline to choose a thumbnail frame", objectName="muted"))

        self.preview = ClipThumbnailPreview()
        self.preview.setMinimumSize(270, 420)
        layout.addWidget(self.preview, 1)

        slider_row = QHBoxLayout()
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, max(1, int(round(self.duration * 20))))
        self.slider.setValue(int(round(self.selected_timestamp * 20)))
        self.time_label = QLabel(f"{self.selected_timestamp:.2f}s / {self.duration:.2f}s", objectName="muted")
        self.slider.valueChanged.connect(self._on_slide)
        slider_row.addWidget(self.slider, 1)
        slider_row.addWidget(self.time_label)
        layout.addLayout(slider_row)

        buttons = QHBoxLayout()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        save = QPushButton("Use This Frame", objectName="primary")
        save.clicked.connect(self.accept)
        buttons.addStretch()
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        layout.addLayout(buttons)

        self._update_preview(self.selected_timestamp)

    def _on_slide(self, value: int) -> None:
        self.selected_timestamp = min(self.duration, value / 20.0)
        self.time_label.setText(f"{self.selected_timestamp:.2f}s / {self.duration:.2f}s")
        self._update_preview(self.selected_timestamp)

    def _update_preview(self, ts: float) -> None:
        try:
            image = extract_frame_at(self.clip_path, ts)
            self.preview.set_thumbnail(image, self.duration)
        except Exception:
            self.preview.set_thumbnail(None, self.duration, status="Frame unavailable")


class ExportThumbnailDialog(QDialog):
    """Options dialog for exporting a 1080x1920 thumbnail with optional title overlay."""

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Export Thumbnail (1080 × 1920)")
        self.resize(400, 180)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)
        layout.addWidget(QLabel(f"Clip: {title}", objectName="sectionTitle"))
        self.overlay_check = QCheckBox("Include clip title overlay (red brand accent & caption font)")
        self.overlay_check.setChecked(True)
        layout.addWidget(self.overlay_check)
        buttons = QHBoxLayout()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        proceed = QPushButton("Choose Save Location…", objectName="primary")
        proceed.clicked.connect(self.accept)
        buttons.addStretch()
        buttons.addWidget(cancel)
        buttons.addWidget(proceed)
        layout.addLayout(buttons)


class ThumbnailTaskRunner(QObject):
    """Bounded background thread pool for non-blocking clip thumbnail generation."""

    completed = Signal(str, object)   # clip_id, ThumbnailResult
    failed = Signal(str, str)         # clip_id, error

    def __init__(self, profile: str = "balanced", parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._executor = ThreadPoolExecutor(max_workers=max_thumbnail_workers(profile), thread_name_prefix="thumb")
        self._inflight: dict[str, Future] = {}
        self._closed = False

    def is_busy(self, clip_id: str) -> bool:
        fut = self._inflight.get(clip_id)
        return fut is not None and not fut.done()

    def submit(
        self,
        clip_id: str,
        clip_path: Path,
        *,
        title: str = "",
        timestamp: float | None = None,
        profile: str = "balanced",
        force: bool = False,
        existing_cache_key: str = "",
    ) -> bool:
        if self._closed or self.is_busy(clip_id):
            return False

        def work() -> ThumbnailResult:
            return generate_clip_thumbnail(
                clip_path,
                clip_path.parent / "thumbnails",
                title=title,
                timestamp=timestamp,
                profile=profile,
                force=force,
                existing_cache_key=existing_cache_key,
            )

        future = self._executor.submit(work)
        self._inflight[clip_id] = future

        def _done(done_future: Future) -> None:
            self._inflight.pop(clip_id, None)
            if self._closed:
                return
            try:
                result = done_future.result()
                self.completed.emit(clip_id, result)
            except Exception as error:
                self.failed.emit(clip_id, str(error))

        future.add_done_callback(_done)
        return True

    def shutdown(self) -> None:
        self._closed = True
        for fut in list(self._inflight.values()):
            fut.cancel()
        self._executor.shutdown(wait=False, cancel_futures=True)
