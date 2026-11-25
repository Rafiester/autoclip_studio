from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QImage
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

from ..media import probe
from ..process import ProcessManager
from ..thumbnails import extract_frame_at
from ..worker import FunctionThread
from .youtube_preview import _Thumbnail, duration_text


LOCAL_VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v"}


@dataclass(frozen=True, slots=True)
class LocalVideoMetadata:
    path: Path
    filename: str
    duration: float
    width: int
    height: int
    fps: float
    file_size: int


def _parse_fps(rate: str) -> float:
    if not rate or "/" not in rate:
        try:
            return float(rate)
        except (TypeError, ValueError):
            return 0.0
    num, den = rate.split("/", 1)
    try:
        d = float(den)
        return float(num) / d if d > 0 else 0.0
    except (TypeError, ValueError):
        return 0.0


def _load_local_preview(video_path: Path, manager: ProcessManager) -> tuple[LocalVideoMetadata, QImage]:
    info = probe(video_path, manager)
    streams = info.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise RuntimeError("The selected file has no video stream.")
    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)
    for side in video.get("side_data_list", []):
        if abs(int(side.get("rotation", 0))) % 180 == 90:
            width, height = height, width
            break
    duration = float(info.get("format", {}).get("duration") or video.get("duration") or 0.0)
    fps = _parse_fps(str(video.get("avg_frame_rate") or "0"))
    file_size = int(video_path.stat().st_size)
    meta = LocalVideoMetadata(
        path=video_path.resolve(),
        filename=video_path.name,
        duration=duration,
        width=width,
        height=height,
        fps=fps,
        file_size=file_size,
    )
    thumb_ts = min(max(0.0, duration * 0.2), 2.0) if duration > 0 else 0.0
    image = extract_frame_at(video_path, thumb_ts)
    return meta, image


def _format_size(num_bytes: int) -> str:
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.0f} KB"
    if num_bytes < 1024 ** 3:
        return f"{num_bytes / (1024 * 1024):.1f} MB"
    return f"{num_bytes / (1024 ** 3):.2f} GB"


class LocalVideoDropZone(QFrame):
    """Compact drag-and-drop and click-to-browse zone for local video selection."""

    file_dropped = Signal(str)
    browse_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("card")
        self.setAcceptDrops(True)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(10)
        self.label = QLabel("Drag and drop a local video here (MP4, MKV, MOV, WEBM, AVI)", objectName="muted")
        self.label.setWordWrap(True)
        self.browse_btn = QPushButton("Browse Video…")
        self.browse_btn.clicked.connect(self.browse_requested.emit)
        layout.addWidget(self.label, 1)
        layout.addWidget(self.browse_btn)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if url.isLocalFile() and Path(url.toLocalFile()).suffix.lower() in LOCAL_VIDEO_EXTENSIONS:
                    event.acceptProposedAction()
                    return
        event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:
        for url in event.mimeData().urls():
            if url.isLocalFile():
                path = Path(url.toLocalFile())
                if path.is_file() and path.suffix.lower() in LOCAL_VIDEO_EXTENSIONS:
                    self.file_dropped.emit(str(path))
                    event.acceptProposedAction()
                    return
        event.ignore()


class LocalVideoPreview(QFrame):
    """Metadata & thumbnail preview card matching YouTubePreview styling."""

    metadata_changed = Signal(object)
    replace_requested = Signal()
    remove_requested = Signal()
    idle = Signal()

    def __init__(self) -> None:
        super().__init__(objectName="videoPreview")
        self.setAcceptDrops(True)
        self.video_path: Path | None = None
        self.result: LocalVideoMetadata | None = None
        self.thread: FunctionThread | None = None
        self.manager: ProcessManager | None = None

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
        self.stats = self._label("videoPreviewDetail")
        self.additional = self._label("videoPreviewDetail")
        self.details = (self.title, self.stats, self.additional)
        for label in self.details:
            body.addWidget(label)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.replace_btn = QPushButton("Replace", objectName="videoPreviewRetry")
        self.replace_btn.clicked.connect(self.replace_requested.emit)
        self.remove_btn = QPushButton("Remove", objectName="danger")
        self.remove_btn.clicked.connect(self.remove_requested.emit)
        actions.addWidget(self.replace_btn)
        actions.addWidget(self.remove_btn)
        actions.addStretch()
        body.addLayout(actions)
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
        if self.manager:
            self.manager.cancel()

    def set_file(self, path_str: str) -> None:
        cleaned = path_str.strip()
        if not cleaned:
            self.clear()
            return
        path = Path(cleaned)
        if not path.is_file():
            self.result = None
            self.video_path = None
            self.metadata_changed.emit(None)
            self.status.setText("Selected video file was not found.")
            self.thumbnail.hide()
            self.skeleton.hide()
            for lbl in self.details:
                lbl.hide()
            self.show()
            return
        if self.video_path == path.resolve() and self.result is not None:
            return
        self.cancel()
        self.video_path = path.resolve()
        self.result = None
        self.metadata_changed.emit(None)
        self.status.setText("Reading local video metadata…")
        self.thumbnail.setVisible(True)
        self.thumbnail.display(None, loading=True)
        self.skeleton.setVisible(True)
        for lbl in self.details:
            lbl.hide()
        self.show()

        manager = ProcessManager()
        self.manager = manager
        target = self.video_path
        self.thread = FunctionThread(lambda: _load_local_preview(target, manager), self, operation="Local video probe")
        self.thread.succeeded.connect(self._ready)
        self.thread.failed.connect(self._failed)
        self.thread.finished.connect(self._finished)
        self.thread.start()

    def clear(self) -> None:
        self.cancel()
        self.video_path = None
        self.result = None
        self.metadata_changed.emit(None)
        self.thumbnail.display(None)
        for lbl in self.details:
            lbl.clear()
        self.hide()

    @Slot(object)
    def _ready(self, preview: tuple[LocalVideoMetadata, QImage]) -> None:
        meta, image = preview
        if self.video_path != meta.path:
            return
        self.result = meta
        clock, duration_words = duration_text(int(round(meta.duration))) if meta.duration > 0 else ("00:00", "0s")
        self.thumbnail.display(image, clock)
        self.status.setText("✓ Local video ready")
        self.title.setText(meta.filename)
        fps_text = f"{meta.fps:.2f} fps" if meta.fps > 0 else "FPS unavailable"
        res_text = f"{meta.width}×{meta.height}" if meta.width and meta.height else "Resolution unavailable"
        self.stats.setText(f"{clock} ({duration_words})  •  {res_text}  •  {fps_text}")
        self.additional.setText(f"File size: {_format_size(meta.file_size)}")
        self.skeleton.hide()
        for lbl in self.details:
            lbl.setVisible(bool(lbl.text()))
        self.metadata_changed.emit(meta)

    @Slot(str)
    def _failed(self, message: str) -> None:
        self.skeleton.hide()
        self.thumbnail.hide()
        self.status.setText(message)

    @Slot()
    def _finished(self) -> None:
        if self.thread is not None:
            self.thread.deleteLater()
            self.thread = None
        self.manager = None
        self.idle.emit()
