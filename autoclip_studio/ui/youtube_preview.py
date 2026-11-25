from __future__ import annotations

from datetime import date

from PySide6.QtCore import QRectF, QSize, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPainterPath
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from ..media import is_youtube_url, youtube_id, youtube_source_error
from ..process import ProcessManager
from ..worker import FunctionThread
from ..youtube_metadata import YouTubeMetadata, fetch_youtube_metadata


def duration_text(duration: int) -> tuple[str, str]:
    minutes, seconds = divmod(duration, 60)
    hours, minutes = divmod(minutes, 60)
    clock = f"{hours:02d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"
    words = f"{hours}h {minutes}m {seconds}s" if hours else f"{minutes}m {seconds}s" if minutes else f"{seconds}s"
    return clock, words


class _Thumbnail(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setFixedSize(240, 135)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self.image = QImage()
        self.clock = ""
        self.loading = False

    def display(self, image: QImage | None, clock: str = "", *, loading: bool = False) -> None:
        self.image = image if image is not None else QImage()
        self.clock = clock
        self.loading = loading
        self.setAccessibleName("Loading video thumbnail" if loading else
                               "Thumbnail unavailable" if self.image.isNull() else "Video thumbnail")
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        bounds = QRectF(self.rect())
        clip = QPainterPath()
        clip.addRoundedRect(bounds, 8, 8)
        painter.setClipPath(clip)
        painter.fillRect(bounds, QColor("#272639" if self.loading else "#10131d"))
        if not self.image.isNull():
            size = self.image.size().scaled(QSize(self.width(), self.height()), Qt.AspectRatioMode.KeepAspectRatio)
            target = QRectF((self.width() - size.width()) / 2, (self.height() - size.height()) / 2,
                            size.width(), size.height())
            painter.drawImage(target, self.image)
        elif not self.loading:
            painter.setPen(QColor("#aeb5c8"))
            painter.drawText(bounds, Qt.AlignmentFlag.AlignCenter, "Thumbnail unavailable")
        if self.clock:
            font = QFont("Consolas")
            font.setPixelSize(13)
            font.setBold(True)
            painter.setFont(font)
            width = painter.fontMetrics().horizontalAdvance(self.clock) + 12
            badge = QRectF(self.width() - width - 7, self.height() - 27, width, 20)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(10, 11, 17, 224))
            painter.drawRoundedRect(badge, 4, 4)
            painter.setPen(QColor("#ffffff"))
            painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, self.clock)


def _load_preview(video_id: str, manager: ProcessManager) -> tuple[YouTubeMetadata, QImage]:
    metadata, thumbnail = fetch_youtube_metadata(video_id, manager)
    image = QImage.fromData(thumbnail)
    return metadata, image


class YouTubePreview(QFrame):
    metadata_changed = Signal(object)
    idle = Signal()

    def __init__(self) -> None:
        super().__init__(objectName="videoPreview")
        self.video_id: str | None = None
        self.result: YouTubeMetadata | None = None
        self.thread: FunctionThread | None = None
        self.manager: ProcessManager | None = None
        self.waiting = False
        self.debounce = QTimer(self, interval=700, singleShot=True, timerType=Qt.TimerType.PreciseTimer)
        self.debounce.timeout.connect(self._fetch)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(15)
        self.thumbnail = _Thumbnail()
        layout.addWidget(self.thumbnail, 0, Qt.AlignmentFlag.AlignTop)
        body = QVBoxLayout()
        body.setSpacing(6)
        self.status = self._label("videoPreviewStatus")
        body.addWidget(self.status)
        self.skeleton = QWidget(objectName="videoPreviewSkeleton")
        bars = QVBoxLayout(self.skeleton)
        bars.setContentsMargins(0, 9, 0, 0)
        bars.setSpacing(12)
        for width, height in ((192, 14), (123, 10), (164, 10)):
            bar = QFrame(objectName="videoPreviewBar")
            bar.setFixedHeight(height)
            bar.setMaximumWidth(width)
            bars.addWidget(bar)
        body.addWidget(self.skeleton)
        self.title = self._label("videoPreviewTitle")
        self.channel = self._label("videoPreviewChannel")
        self.stats = self._label("videoPreviewDetail")
        self.additional = self._label("videoPreviewDetail")
        self.details = (self.title, self.channel, self.stats, self.additional)
        for label in self.details:
            body.addWidget(label)
        self.retry = QPushButton("Retry", objectName="videoPreviewRetry")
        self.retry.setAccessibleName("Retry YouTube video detection")
        self.retry.clicked.connect(self._fetch)
        body.addWidget(self.retry, 0, Qt.AlignmentFlag.AlignLeft)
        body.addStretch()
        layout.addLayout(body, 1)
        self.hide()

    @staticmethod
    def _label(name: str) -> QLabel:
        label = QLabel(objectName=name, textFormat=Qt.TextFormat.PlainText, wordWrap=True)
        policy = label.sizePolicy()
        policy.setHorizontalPolicy(QSizePolicy.Policy.Ignored)
        label.setSizePolicy(policy)
        return label

    @property
    def busy(self) -> bool:
        return self.thread is not None

    def cancel(self) -> None:
        self.debounce.stop()
        self.waiting = False
        if self.manager:
            self.manager.cancel()

    @Slot(str)
    def set_source(self, value: str) -> None:
        url = value.strip()
        valid_host = is_youtube_url(url)
        video_id = youtube_id(url) if valid_host else None
        if video_id and video_id == self.video_id:
            return
        self.cancel()
        self.video_id = video_id
        self.result = None
        self.metadata_changed.emit(None)
        self.hide()
        self.thumbnail.display(None)
        for label in self.details:
            label.clear()
        if video_id:
            self.debounce.start()
        elif valid_host:
            self._message(youtube_source_error(url) or "Paste the direct video link to preview this livestream.")
        elif "://" in url or url.lower().startswith(("http:", "https:", "www.youtube.", "youtube.com", "youtu.be")):
            self._message("Enter a complete YouTube URL or browse for a local video.")

    def _message(self, message: str, *, retry: bool = False, loading: bool = False) -> None:
        self.status.setText(message)
        self.retry.setVisible(retry)
        self.thumbnail.setVisible(loading)
        self.skeleton.setVisible(loading)
        for label in self.details:
            label.hide()
        if loading:
            self.thumbnail.display(None, loading=True)
        self.show()

    @Slot()
    def _fetch(self) -> None:
        if not self.video_id:
            return
        self._message("Detecting YouTube video…", loading=True)
        if self.thread:
            self.waiting = True
            return
        self.waiting = False
        video_id, manager = self.video_id, ProcessManager()
        self.manager = manager
        self.thread = FunctionThread(lambda: _load_preview(video_id, manager), self, operation="Video detection")
        self.thread.succeeded.connect(self._ready)
        self.thread.failed.connect(self._failed)
        self.thread.finished.connect(self._finished)
        self.thread.start()

    @Slot(object)
    def _ready(self, preview: tuple[YouTubeMetadata, QImage]) -> None:
        metadata, image = preview
        if not self.manager or self.manager.cancel_event.is_set() or metadata.video_id != self.video_id:
            return
        self.result = metadata
        clock, duration = duration_text(metadata.duration) if metadata.duration is not None else ("", "")
        self.thumbnail.display(image, clock)
        self.status.setText("✓ Video detected")
        self.title.setText(metadata.title)
        self.channel.setText(metadata.channel)
        self.stats.setText("  •  ".join(filter(None, (
            f"{clock} ({duration})" if duration else "Duration unavailable",
            f"Up to {metadata.resolution}" if metadata.resolution else "Resolution unavailable",
        ))))
        upload = date.fromisoformat(metadata.upload_date).strftime("%d %b %Y") if metadata.upload_date else ""
        views = f"{metadata.view_count:,} views" if metadata.view_count is not None else ""
        self.additional.setText("  •  ".join(filter(None, (views, upload))))
        self.skeleton.hide()
        for label in self.details:
            label.setVisible(bool(label.text()))
        self.metadata_changed.emit(metadata)

    @Slot(str)
    def _failed(self, message: str) -> None:
        if self.manager and not self.manager.cancel_event.is_set():
            self._message(message, retry=True)

    @Slot()
    def _finished(self) -> None:
        self.thread.deleteLater()
        self.thread = None
        self.manager = None
        if self.waiting:
            self._fetch()
        else:
            self.idle.emit()
