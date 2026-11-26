from __future__ import annotations

import math
from pathlib import Path
from typing import Callable

import cv2
from PySide6.QtCore import QEvent, QObject, QRectF, QSize, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QAction, QColor, QImage, QKeySequence, QPainter, QPainterPath, QResizeEvent, QShortcut
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QMenu, QProgressBar, QPushButton, QScrollArea, QSizePolicy, QSlider,
    QStackedWidget, QVBoxLayout, QWidget,
)

from .clips_view import ClipThumbnailPreview, _format_duration
from .icons import lucide_icon
from .theme import theme_manager


_PROBE_CACHE: dict[tuple[str, int], tuple[int, int, int]] = {}


def probe_clip_metrics(path_str: str) -> tuple[int, int, int]:
    """Return (width, height, file_bytes) for a clip file with mtime caching."""
    path = Path(path_str)
    try:
        stat = path.stat()
    except OSError:
        return (0, 0, 0)
    key = (str(path), stat.st_mtime_ns)
    if key in _PROBE_CACHE:
        return _PROBE_CACHE[key]
    width = height = 0
    cap = cv2.VideoCapture(str(path))
    try:
        if cap.isOpened():
            width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
            height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    finally:
        cap.release()
    result = (width, height, int(stat.st_size))
    if len(_PROBE_CACHE) > 512:
        _PROBE_CACHE.clear()
    _PROBE_CACHE[key] = result
    return result


def format_aspect_ratio(width: int, height: int) -> str:
    if width <= 0 or height <= 0:
        return "9:16"
    divisor = math.gcd(width, height)
    rw, rh = width // divisor, height // divisor
    if rw > 20 or rh > 20:
        ratio = width / height
        for label, target in (("9:16", 9 / 16), ("4:5", 4 / 5), ("1:1", 1.0), ("16:9", 16 / 9)):
            if abs(ratio - target) < 0.04:
                return label
        return f"{ratio:.2f}:1"
    return f"{rw}:{rh}"


def format_file_size(num_bytes: int) -> str:
    if num_bytes <= 0:
        return "—"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.0f} KB"
    return f"{num_bytes / (1024 * 1024):.1f} MB"


class _CroppedThumb(QWidget):
    """Lightweight cropped 9:16 thumbnail image with optional duration badge."""

    def __init__(self, width: int, height: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(width, height)
        self._image = QImage()
        self._loading = False
        self._duration = ""

    def set_data(self, thumb_path: str, duration: float, *, loading: bool = False) -> None:
        self._loading = loading
        self._duration = _format_duration(duration) if duration > 0 else ""
        path = Path(thumb_path) if thumb_path else None
        self._image = QImage(str(path)) if path and path.is_file() else QImage()
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        bounds = QRectF(self.rect())
        clip = QPainterPath()
        clip.addRoundedRect(bounds, 8, 8)
        painter.setClipPath(clip)
        painter.fillRect(bounds, QColor("#22252E" if self._loading else "#101116"))

        if not self._image.isNull():
            scaled = self._image.scaled(
                int(bounds.width()), int(bounds.height()),
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
            sx = max(0.0, (scaled.width() - bounds.width()) / 2.0)
            sy = max(0.0, (scaled.height() - bounds.height()) / 2.0)
            painter.drawImage(bounds, scaled, QRectF(sx, sy, bounds.width(), bounds.height()))
        else:
            painter.setPen(QColor("#A3A8B5"))
            painter.drawText(bounds, Qt.AlignmentFlag.AlignCenter, "…" if self._loading else "No thumb")

        if self._duration:
            font = painter.font()
            font.setFamily("Consolas")
            font.setPixelSize(11)
            font.setBold(True)
            painter.setFont(font)
            tw = painter.fontMetrics().horizontalAdvance(self._duration) + 10
            badge = QRectF(bounds.width() - tw - 5, bounds.height() - 22, tw, 17)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(16, 17, 22, 224))
            painter.drawRoundedRect(badge, 4, 4)
            painter.setPen(QColor("#F5F5F7"))
            painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, self._duration)


class ClipLibraryCard(QFrame):
    """Individual clip card supporting both List View and Grid View."""

    clicked = Signal(str)

    def __init__(self, clip: dict, mode: str, status_text: str, *,
                 loading: bool = False, selected: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("clipLibraryCard")
        self.clip_id = str(clip["id"])
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(f"{clip['title']}\nFile: {Path(clip['path']).name}")
        self.setProperty("selected", "true" if selected else "false")

        thumb_path = str(clip.get("thumbnail_path") or "")
        duration = float(clip.get("duration") or 0.0)
        score = int(clip.get("score") or 0)
        title = str(clip.get("title") or "Untitled clip")
        desc = str(clip.get("description") or "").strip()

        if mode == "grid":
            self.setFixedWidth(182)
            self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            root = QVBoxLayout(self)
            root.setContentsMargins(10, 10, 10, 10)
            root.setSpacing(8)
            thumb = _CroppedThumb(162, 240, self)
            thumb.set_data(thumb_path, duration, loading=loading)
            root.addWidget(thumb, alignment=Qt.AlignmentFlag.AlignCenter)

            title_lbl = QLabel(title)
            title_lbl.setWordWrap(True)
            title_lbl.setMaximumHeight(38)
            title_lbl.setStyleSheet("font-weight: 600; font-size: 13px;")
            root.addWidget(title_lbl)

            badges = QHBoxLayout()
            badges.setSpacing(6)
            score_lbl = QLabel(f"★ {score}", objectName="scorePill")
            status_lbl = QLabel(status_text, objectName="statusChip")
            badges.addWidget(score_lbl)
            badges.addStretch()
            badges.addWidget(status_lbl)
            root.addLayout(badges)
        else:
            self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            root = QHBoxLayout(self)
            root.setContentsMargins(10, 10, 12, 10)
            root.setSpacing(12)

            thumb = _CroppedThumb(54, 96, self)
            thumb.set_data(thumb_path, duration, loading=loading)
            root.addWidget(thumb)

            info = QVBoxLayout()
            info.setSpacing(6)
            title_lbl = QLabel(title)
            title_lbl.setWordWrap(True)
            title_lbl.setStyleSheet("font-weight: 600; font-size: 14px;")
            info.addWidget(title_lbl)

            if desc:
                desc_lbl = QLabel(desc, objectName="muted")
                desc_lbl.setWordWrap(False)
                desc_lbl.setMaximumHeight(20)
                info.addWidget(desc_lbl)

            meta = QHBoxLayout()
            meta.setSpacing(8)
            score_lbl = QLabel(f"Score {score}/100", objectName="scorePill")
            dur_lbl = QLabel(_format_duration(duration), objectName="statusChip")
            status_lbl = QLabel(status_text, objectName="statusChip")
            meta.addWidget(score_lbl)
            meta.addWidget(dur_lbl)
            meta.addWidget(status_lbl)
            meta.addStretch()
            info.addLayout(meta)
            root.addLayout(info, 1)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.clip_id)
        super().mousePressEvent(event)

    def set_selected(self, selected: bool) -> None:
        val = "true" if selected else "false"
        if self.property("selected") != val:
            self.setProperty("selected", val)
            self.style().unpolish(self)
            self.style().polish(self)
            self.update()


class ClipLibraryPanel(QWidget):
    """Master pane with summary metrics, search/sort/view toolbar, and scrollable clip cards."""

    clip_selected = Signal(dict)
    export_all_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._all_clips: list[dict] = []
        self._filtered_clips: list[dict] = []
        self._busy_ids: set[str] = set()
        self._selected_id: str | None = None
        self._view_mode = "list"
        self._cards: dict[str, ClipLibraryCard] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)

        # Compact summary strip
        summary_row = QHBoxLayout()
        summary_row.setSpacing(8)
        self.summary_count = self._summary_pill("Total clips", "0")
        self.summary_duration = self._summary_pill("Combined duration", "00:00")
        self.summary_score = self._summary_pill("Avg AI score", "—")
        for pill in (self.summary_count, self.summary_duration, self.summary_score):
            summary_row.addWidget(pill, 1)
        root.addLayout(summary_row)

        # Functional toolbar
        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search clips…")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.setAccessibleName("Search generated clips")
        self.search_input.addAction(lucide_icon("search", "#A3A8B5", 14), QLineEdit.ActionPosition.LeadingPosition)
        self.search_input.textChanged.connect(self._rebuild_cards)

        self.sort_combo = QComboBox()
        self.sort_combo.addItems(["Newest first", "Highest score", "Longest duration", "Title (A–Z)"])
        self.sort_combo.setAccessibleName("Sort generated clips")
        self.sort_combo.currentIndexChanged.connect(self._rebuild_cards)

        self.list_view_btn = QPushButton(objectName="viewToggle")
        self.list_view_btn.setToolTip("List View")
        self.list_view_btn.setAccessibleName("List View")
        self.list_view_btn.setCheckable(True)
        self.list_view_btn.setChecked(True)
        self.list_view_btn.clicked.connect(lambda: self.set_view_mode("list"))

        self.grid_view_btn = QPushButton(objectName="viewToggle")
        self.grid_view_btn.setToolTip("Grid View")
        self.grid_view_btn.setAccessibleName("Grid View")
        self.grid_view_btn.setCheckable(True)
        self.grid_view_btn.clicked.connect(lambda: self.set_view_mode("grid"))

        self.export_all_btn = QPushButton("Export All")
        self.export_all_btn.clicked.connect(self.export_all_requested.emit)
        self.export_all_btn.setEnabled(False)
        self.apply_theme()

        toolbar.addWidget(self.search_input, 2)
        toolbar.addWidget(self.sort_combo, 1)
        root.addLayout(toolbar)

        controls_row = QHBoxLayout()
        controls_row.setSpacing(8)
        controls_row.addWidget(self.list_view_btn)
        controls_row.addWidget(self.grid_view_btn)
        controls_row.addStretch()
        controls_row.addWidget(self.export_all_btn)
        root.addLayout(controls_row)

        # Independently scrollable library
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._container = QWidget()
        self._container_layout = QVBoxLayout(self._container)
        self._container_layout.setContentsMargins(0, 0, 4, 0)
        self._container_layout.setSpacing(8)
        self.scroll.setWidget(self._container)
        root.addWidget(self.scroll, 1)

    @staticmethod
    def _summary_pill(label: str, value: str) -> QFrame:
        pill = QFrame(objectName="summaryPill")
        lay = QVBoxLayout(pill)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(2)
        lay.addWidget(QLabel(label, objectName="muted"))
        val = QLabel(value, objectName="summaryValue")
        lay.addWidget(val)
        return pill

    def apply_theme(self, _resolved: str = "") -> None:
        fg = theme_manager().tokens["text_primary"]
        self.list_view_btn.setIcon(lucide_icon("list", fg, 15))
        self.grid_view_btn.setIcon(lucide_icon("grid", fg, 15))
        self.export_all_btn.setIcon(lucide_icon("download", fg, 15))

    def set_view_mode(self, mode: str) -> None:
        normalized = "grid" if mode == "grid" else "list"
        self.list_view_btn.setChecked(normalized == "list")
        self.grid_view_btn.setChecked(normalized == "grid")
        if self._view_mode != normalized:
            self._view_mode = normalized
            self._rebuild_cards()

    def set_clips(self, clips: list[dict], *, busy_ids: set[str] | None = None) -> None:
        self._all_clips = list(clips)
        if busy_ids is not None:
            self._busy_ids = set(busy_ids)
        self._update_summary()
        self._rebuild_cards()

    def set_clip_busy(self, clip_id: str, busy: bool) -> None:
        if busy:
            self._busy_ids.add(clip_id)
        else:
            self._busy_ids.discard(clip_id)
        self._rebuild_cards()

    def _update_summary(self) -> None:
        total = len(self._all_clips)
        total_dur = sum(float(c.get("duration") or 0.0) for c in self._all_clips)
        avg_score = round(sum(int(c.get("score") or 0) for c in self._all_clips) / total) if total else 0
        self.summary_count.findChild(QLabel, "summaryValue").setText(str(total))
        self.summary_duration.findChild(QLabel, "summaryValue").setText(_format_duration(total_dur))
        self.summary_score.findChild(QLabel, "summaryValue").setText(f"{avg_score}/100" if total else "—")
        self.export_all_btn.setEnabled(total > 0)

    def _sorted_filtered(self) -> list[dict]:
        query = self.search_input.text().strip().lower()
        items = [
            c for c in self._all_clips
            if not query or query in str(c.get("title", "")).lower() or query in str(c.get("description", "")).lower()
        ]
        sort_idx = self.sort_combo.currentIndex()
        if sort_idx == 1:
            items.sort(key=lambda c: int(c.get("score") or 0), reverse=True)
        elif sort_idx == 2:
            items.sort(key=lambda c: float(c.get("duration") or 0.0), reverse=True)
        elif sort_idx == 3:
            items.sort(key=lambda c: str(c.get("title") or "").lower())
        else:
            items.sort(key=lambda c: str(c.get("created_at") or ""), reverse=True)
        return items

    def _clip_status(self, clip: dict) -> tuple[str, bool]:
        if not Path(clip["path"]).is_file():
            return "File missing", False
        if clip["id"] in self._busy_ids:
            return "Generating…", True
        thumb_path = Path(clip.get("thumbnail_path") or "")
        if thumb_path.is_file():
            return "Ready", False
        return "Thumbnail pending", True

    def _clear_container(self) -> None:
        self._cards.clear()
        while self._container_layout.count():
            item = self._container_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                self._clear_sublayout(item.layout())

    @staticmethod
    def _clear_sublayout(lay) -> None:
        while lay.count():
            child = lay.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
            elif child.layout():
                ClipLibraryPanel._clear_sublayout(child.layout())

    def _rebuild_cards(self) -> None:
        self._clear_container()
        self._filtered_clips = self._sorted_filtered()

        if not self._filtered_clips:
            empty = QFrame(objectName="card")
            elay = QVBoxLayout(empty)
            elay.setContentsMargins(24, 32, 24, 32)
            elay.setSpacing(6)
            msg = "No clips match your search." if self._all_clips else "No generated clips yet."
            lbl = QLabel(msg, objectName="sectionTitle")
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            sub = QLabel("Create a project to generate short-form vertical clips.", objectName="muted")
            sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
            elay.addWidget(lbl)
            elay.addWidget(sub)
            self._container_layout.addWidget(empty)
            self._container_layout.addStretch()
            self._selected_id = None
            self.clip_selected.emit({})
            return

        valid_ids = {str(c["id"]) for c in self._filtered_clips}
        if self._selected_id not in valid_ids:
            self._selected_id = str(self._filtered_clips[0]["id"])

        if self._view_mode == "grid":
            grid_host = QWidget()
            grid = QGridLayout(grid_host)
            grid.setContentsMargins(0, 0, 0, 0)
            grid.setSpacing(10)
            cols = max(1, (self.scroll.viewport().width() - 12) // 192)
            for idx, clip in enumerate(self._filtered_clips):
                status, loading = self._clip_status(clip)
                card = ClipLibraryCard(
                    clip, "grid", status, loading=loading,
                    selected=(str(clip["id"]) == self._selected_id),
                )
                card.clicked.connect(self.select_clip_by_id)
                self._cards[str(clip["id"])] = card
                r, c = divmod(idx, cols)
                grid.addWidget(card, r, c)
            grid.setColumnStretch(cols, 1)
            self._container_layout.addWidget(grid_host)
        else:
            for clip in self._filtered_clips:
                status, loading = self._clip_status(clip)
                card = ClipLibraryCard(
                    clip, "list", status, loading=loading,
                    selected=(str(clip["id"]) == self._selected_id),
                )
                card.clicked.connect(self.select_clip_by_id)
                self._cards[str(clip["id"])] = card
                self._container_layout.addWidget(card)

        self._container_layout.addStretch()
        current = self.selected_clip()
        if current:
            self.clip_selected.emit(current)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if self._view_mode == "grid" and self._filtered_clips:
            self._rebuild_cards()

    def select_clip_by_id(self, clip_id: str) -> None:
        self._selected_id = clip_id
        for cid, card in self._cards.items():
            card.set_selected(cid == clip_id)
        clip = self.selected_clip()
        if clip:
            self.clip_selected.emit(clip)

    def select_row(self, index: int) -> None:
        if 0 <= index < len(self._filtered_clips):
            self.select_clip_by_id(str(self._filtered_clips[index]["id"]))

    def selected_clip(self) -> dict | None:
        if not self._selected_id:
            return None
        return next((c for c in self._filtered_clips if str(c["id"]) == self._selected_id), None)

    def card_count(self) -> int:
        return len(self._filtered_clips)


class AspectVideoStage(QFrame):
    """Centered 9:16 video/thumbnail stage that preserves aspect ratio without stretching."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("playerStage")
        self.setMinimumSize(200, 260)
        self._aspect = 9.0 / 16.0

        self.stack = QStackedWidget(self)
        self.thumb_preview = ClipThumbnailPreview()
        self.video_widget = QVideoWidget()
        self.video_widget.setAspectRatioMode(Qt.AspectRatioMode.KeepAspectRatio)
        self.stack.addWidget(self.thumb_preview)
        self.stack.addWidget(self.video_widget)

    def set_aspect_ratio(self, width: int, height: int) -> None:
        if width > 0 and height > 0:
            self._aspect = width / float(height)
        else:
            self._aspect = 9.0 / 16.0
        self._layout_stage()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._layout_stage()

    def _layout_stage(self) -> None:
        pad = 8
        avail_w = max(80, self.width() - pad * 2)
        avail_h = max(80, self.height() - pad * 2)
        if avail_w / avail_h > self._aspect:
            h = avail_h
            w = int(round(h * self._aspect))
        else:
            w = avail_w
            h = int(round(w / max(0.1, self._aspect)))
        x = (self.width() - w) // 2
        y = (self.height() - h) // 2
        self.stack.setGeometry(x, y, w, h)


class _FullscreenVideoDialog(QDialog):
    """Lightweight fullscreen modal container that hosts the single QVideoWidget cleanly."""

    def __init__(self, video_widget: QVideoWidget, on_toggle_play: Callable[[], None], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("AutoClip Studio — Clip Preview")
        self.setStyleSheet("background: #08090C;")
        self._video = video_widget
        self._on_toggle_play = on_toggle_play
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(video_widget)
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, activated=self.accept)
        QShortcut(QKeySequence(Qt.Key.Key_F), self, activated=self.accept)
        QShortcut(QKeySequence(Qt.Key.Key_Space), self, activated=self._on_toggle_play)


class ClipDetailPlayerPanel(QFrame):
    """Right-side professional 9:16 video player, simplified action bar, and clip metadata panel."""

    open_file_requested = Signal()
    open_folder_requested = Signal()
    regenerate_thumb_requested = Signal()
    select_frame_requested = Signal()
    export_thumb_requested = Signal()
    rename_clip_requested = Signal()
    delete_clip_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("card")
        self._current_clip: dict | None = None
        self._seeking = False
        self._duration_ms = 0
        self._fullscreen_dialog: _FullscreenVideoDialog | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        # 9:16 centered stage
        self.stage = AspectVideoStage(self)
        root.addWidget(self.stage, 1)

        # Single shared media player
        self.audio_output = QAudioOutput(self)
        self.audio_output.setVolume(0.8)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio_output)
        self.player.setVideoOutput(self.stage.video_widget)
        self.player.positionChanged.connect(self._on_position)
        self.player.durationChanged.connect(self._on_duration)
        self.player.playbackStateChanged.connect(self._on_state_changed)
        self.player.errorOccurred.connect(self._on_error)

        # Seekable timeline
        timeline = QHBoxLayout()
        timeline.setSpacing(8)
        self.time_current = QLabel("00:00", objectName="muted")
        self.seek_slider = QSlider(Qt.Orientation.Horizontal)
        self.seek_slider.setRange(0, 0)
        self.seek_slider.setAccessibleName("Playback timeline")
        self.seek_slider.sliderPressed.connect(lambda: setattr(self, "_seeking", True))
        self.seek_slider.sliderReleased.connect(self._finish_seek)
        self.seek_slider.sliderMoved.connect(lambda pos: self.time_current.setText(_format_duration(pos / 1000.0)))
        self.time_total = QLabel("00:00", objectName="muted")
        timeline.addWidget(self.time_current)
        timeline.addWidget(self.seek_slider, 1)
        timeline.addWidget(self.time_total)
        root.addLayout(timeline)

        # Transport controls
        transport = QHBoxLayout()
        transport.setSpacing(6)
        self.skip_back_btn = QPushButton(objectName="iconButton")
        self.skip_back_btn.setIcon(lucide_icon("skip-back", "#F5F5F7", 15))
        self.skip_back_btn.setToolTip("Back 5s (Left / J)")
        self.skip_back_btn.clicked.connect(lambda: self.seek_relative(-5000))

        self.play_btn = QPushButton(objectName="iconButton")
        self.play_btn.setIcon(lucide_icon("play", "#F5F5F7", 16))
        self.play_btn.setToolTip("Play / Pause (Space / K)")
        self.play_btn.clicked.connect(self.toggle_play)

        self.skip_fwd_btn = QPushButton(objectName="iconButton")
        self.skip_fwd_btn.setIcon(lucide_icon("skip-forward", "#F5F5F7", 15))
        self.skip_fwd_btn.setToolTip("Forward 5s (Right / L)")
        self.skip_fwd_btn.clicked.connect(lambda: self.seek_relative(5000))

        self.mute_btn = QPushButton(objectName="iconButton")
        self.mute_btn.setIcon(lucide_icon("volume-2", "#F5F5F7", 15))
        self.mute_btn.setToolTip("Mute / Unmute (M)")
        self.mute_btn.clicked.connect(self.toggle_mute)

        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(80)
        self.volume_slider.setFixedWidth(72)
        self.volume_slider.setAccessibleName("Volume")
        self.volume_slider.valueChanged.connect(self._on_volume_changed)

        self.fullscreen_btn = QPushButton(objectName="iconButton")
        self.fullscreen_btn.setIcon(lucide_icon("maximize", "#F5F5F7", 15))
        self.fullscreen_btn.setToolTip("Fullscreen (F)")
        self.fullscreen_btn.clicked.connect(self.toggle_fullscreen)

        for b in (self.skip_back_btn, self.play_btn, self.skip_fwd_btn, self.mute_btn, self.fullscreen_btn):
            b.setEnabled(False)

        transport.addWidget(self.skip_back_btn)
        transport.addWidget(self.play_btn)
        transport.addWidget(self.skip_fwd_btn)
        transport.addStretch()
        transport.addWidget(self.mute_btn)
        transport.addWidget(self.volume_slider)
        transport.addWidget(self.fullscreen_btn)
        root.addLayout(transport)

        # Clip info panel
        info_box = QVBoxLayout()
        info_box.setSpacing(8)
        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        self.title_label = QLabel("Select a clip", objectName="sectionTitle")
        self.title_label.setWordWrap(True)
        self.score_badge = QLabel("—", objectName="scorePill")
        title_row.addWidget(self.title_label, 1)
        title_row.addWidget(self.score_badge)
        info_box.addLayout(title_row)

        self.score_bar = QProgressBar()
        self.score_bar.setRange(0, 100)
        self.score_bar.setValue(0)
        self.score_bar.setFixedHeight(5)
        self.score_bar.setTextVisible(False)
        info_box.addWidget(self.score_bar)

        score_breakdown = QHBoxLayout()
        score_breakdown.setSpacing(6)
        self.ai_score_chip = QLabel("AI: —", objectName="statusChip")
        self.replay_score_chip = QLabel("Replay: —", objectName="statusChip")
        self.mode_chip = QLabel("Mode: —", objectName="statusChip")
        for chip in (self.ai_score_chip, self.replay_score_chip, self.mode_chip):
            score_breakdown.addWidget(chip)
        score_breakdown.addStretch()
        info_box.addLayout(score_breakdown)

        self.specs_label = QLabel("—", objectName="muted")
        self.specs_label.setWordWrap(True)
        self.desc_label = QLabel("Select a clip from the library to inspect and play.", objectName="muted")
        self.desc_label.setWordWrap(True)
        self.reason_label = QLabel("", objectName="muted")
        self.reason_label.setWordWrap(True)
        self.reason_label.hide()
        info_box.addWidget(self.specs_label)
        info_box.addWidget(self.desc_label)
        info_box.addWidget(self.reason_label)
        root.addLayout(info_box)

        # Simplified action bar: 1 primary CTA + 1 overflow menu
        actions_row = QHBoxLayout()
        actions_row.setSpacing(8)
        self.primary_btn = QPushButton("Open File", objectName="primary")
        self.primary_btn.setIcon(lucide_icon("external-link", "#F5F5F7", 15))
        self.primary_btn.clicked.connect(self.open_file_requested.emit)
        self.primary_btn.setEnabled(False)

        self.more_btn = QPushButton(objectName="iconButton")
        self.more_btn.setIcon(lucide_icon("more-horizontal", "#F5F5F7", 16))
        self.more_btn.setText(" Actions")
        self.more_btn.setAccessibleName("Clip actions menu")
        self.more_btn.setEnabled(False)

        self.actions_menu = QMenu(self)
        self.action_open_file = self.actions_menu.addAction(lucide_icon("external-link", "#F5F5F7", 14), "Open File", self.open_file_requested.emit)
        self.action_open_folder = self.actions_menu.addAction(lucide_icon("folder-open", "#F5F5F7", 14), "Open Containing Folder", self.open_folder_requested.emit)
        self.actions_menu.addSeparator()
        self.action_regen_thumb = self.actions_menu.addAction(lucide_icon("refresh-cw", "#F5F5F7", 14), "Regenerate Thumbnail", self.regenerate_thumb_requested.emit)
        self.action_pick_frame = self.actions_menu.addAction(lucide_icon("image", "#F5F5F7", 14), "Select Thumbnail Frame…", self.select_frame_requested.emit)
        self.action_export_thumb = self.actions_menu.addAction(lucide_icon("download", "#F5F5F7", 14), "Export Thumbnail…", self.export_thumb_requested.emit)
        self.action_rename_clip = self.actions_menu.addAction(lucide_icon("edit-3", "#F5F5F7", 14), "Rename Clip…", self.rename_clip_requested.emit)
        self.actions_menu.addSeparator()
        self.action_delete_clip = self.actions_menu.addAction(lucide_icon("trash-2", "#FF9AA2", 14), "Delete Clip", self.delete_clip_requested.emit)
        self.more_btn.setMenu(self.actions_menu)

        actions_row.addWidget(self.primary_btn, 1)
        actions_row.addWidget(self.more_btn)
        root.addLayout(actions_row)

        # Keyboard shortcuts scoped to this panel
        for key, fn in (
            (Qt.Key.Key_Space, self.toggle_play),
            (Qt.Key.Key_K, self.toggle_play),
            (Qt.Key.Key_Left, lambda: self.seek_relative(-5000)),
            (Qt.Key.Key_J, lambda: self.seek_relative(-5000)),
            (Qt.Key.Key_Right, lambda: self.seek_relative(5000)),
            (Qt.Key.Key_L, lambda: self.seek_relative(5000)),
            (Qt.Key.Key_M, self.toggle_mute),
            (Qt.Key.Key_F, self.toggle_fullscreen),
        ):
            sc = QShortcut(QKeySequence(key), self)
            sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            sc.activated.connect(fn)

    def display_clip(self, clip: dict | None, *, busy: bool = False) -> None:
        self.stop_playback()
        self._current_clip = clip if clip and clip.get("id") else None
        if not self._current_clip:
            self.player.setSource(QUrl())
            self.stage.set_aspect_ratio(9, 16)
            self.stage.stack.setCurrentIndex(0)
            self.stage.thumb_preview.set_thumbnail(None, 0.0, status="Select a clip to preview")
            self.title_label.setText("Select a clip")
            self.score_badge.setText("—")
            self.score_bar.setValue(0)
            self.ai_score_chip.setText("AI: —")
            self.replay_score_chip.setText("Replay: —")
            self.mode_chip.setText("Mode: —")
            self.specs_label.setText("—")
            self.desc_label.setText("Select a clip from the library to inspect and play.")
            self.reason_label.hide()
            self.time_current.setText("00:00")
            self.time_total.setText("00:00")
            self.seek_slider.setRange(0, 0)
            self._set_controls_enabled(False, file_exists=False)
            return

        clip_path = Path(self._current_clip["path"])
        exists = clip_path.is_file()
        duration = float(self._current_clip.get("duration") or 0.0)
        score = int(self._current_clip.get("score") or 0)

        self.title_label.setText(str(self._current_clip["title"]))
        ai_score = self._current_clip.get("ai_score")
        replay_score = self._current_clip.get("replay_score")
        mode_raw = str(self._current_clip.get("detection_mode") or "ai")
        mode_names = {"hybrid": "Hybrid", "most_replayed": "Most Replayed", "ai": "AI Analysis"}
        self.score_badge.setText(f"Combined {score}/100" if replay_score is not None else f"Score {score}/100")
        self.score_bar.setValue(max(0, min(100, score)))
        self.ai_score_chip.setText(f"AI: {int(ai_score)}/100" if ai_score is not None else f"AI: {score}/100")
        self.replay_score_chip.setText(f"Replay: {int(replay_score)}/100" if replay_score is not None else "Replay: N/A")
        self.mode_chip.setText(f"Mode: {mode_names.get(mode_raw, mode_raw)}")
        reason_text = str(self._current_clip.get("reason") or "").strip()
        if reason_text:
            self.reason_label.setText(f"Why selected: {reason_text}")
            self.reason_label.show()
        else:
            self.reason_label.hide()
        self.time_current.setText("00:00")
        self.time_total.setText(_format_duration(duration))

        if not exists:
            self.player.setSource(QUrl())
            self.stage.stack.setCurrentIndex(0)
            self.stage.thumb_preview.set_thumbnail(None, duration, status="Clip file missing")
            self.specs_label.setText(f"Duration {_format_duration(duration)} • Missing file ({clip_path.name})")
            self.desc_label.setText("The exported clip file could not be found on disk.")
            self._set_controls_enabled(True, file_exists=False)
            return

        width, height, size_bytes = probe_clip_metrics(str(clip_path))
        self.stage.set_aspect_ratio(width, height)
        aspect = format_aspect_ratio(width, height)
        res_text = f"{width}×{height}" if width and height else "Unknown"
        thumb_path = Path(self._current_clip.get("thumbnail_path") or "")
        if thumb_path.is_file() and not busy:
            self.stage.thumb_preview.set_thumbnail(QImage(str(thumb_path)), duration)
            thumb_status = "Thumbnail ready"
        else:
            self.stage.thumb_preview.set_thumbnail(None, duration, loading=True)
            thumb_status = "Generating thumbnail…" if busy else "Thumbnail pending"

        self.specs_label.setText(
            f"{_format_duration(duration)} • {res_text} ({aspect}) • {format_file_size(size_bytes)} • {thumb_status}"
        )
        self.desc_label.setText(str(self._current_clip.get("description") or "").strip() or clip_path.name)
        self.stage.stack.setCurrentIndex(0)

        source = QUrl.fromLocalFile(str(clip_path))
        if source != self.player.source():
            self.player.setSource(source)
        self._set_controls_enabled(True, file_exists=True)

    def _set_controls_enabled(self, has_clip: bool, *, file_exists: bool) -> None:
        for b in (self.skip_back_btn, self.play_btn, self.skip_fwd_btn, self.mute_btn,
                  self.fullscreen_btn, self.primary_btn):
            b.setEnabled(has_clip and file_exists)
        self.more_btn.setEnabled(has_clip)
        for act in (self.action_open_file, self.action_open_folder, self.action_regen_thumb,
                    self.action_pick_frame, self.action_export_thumb):
            act.setEnabled(has_clip and file_exists)
        self.action_rename_clip.setEnabled(has_clip)
        self.action_delete_clip.setEnabled(has_clip)

    def apply_theme(self, _resolved: str = "") -> None:
        fg = theme_manager().tokens["text_primary"]
        self.skip_back_btn.setIcon(lucide_icon("skip-back", fg, 15))
        playing = self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        self.play_btn.setIcon(lucide_icon("pause" if playing else "play", fg, 15))
        self.skip_fwd_btn.setIcon(lucide_icon("skip-forward", fg, 15))
        self.mute_btn.setIcon(lucide_icon("volume-x" if self.audio_output.isMuted() else "volume-2", fg, 15))
        self.fullscreen_btn.setIcon(lucide_icon("maximize", fg, 15))
        self.more_btn.setIcon(lucide_icon("more-horizontal", fg, 16))
        self.primary_btn.setIcon(lucide_icon("external-link", "#FFFFFF", 15))

    def stop_playback(self) -> None:
        if self._fullscreen_dialog is not None:
            self._fullscreen_dialog.accept()
        if self.player.playbackState() != QMediaPlayer.PlaybackState.StoppedState:
            self.player.stop()
        self.stage.stack.setCurrentIndex(0)
        self.play_btn.setIcon(lucide_icon("play", "#F5F5F7", 16))

    def toggle_play(self) -> None:
        if not self.play_btn.isEnabled():
            return
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.stage.stack.setCurrentIndex(1)
            self.player.play()

    def seek_relative(self, delta_ms: int) -> None:
        if not self.play_btn.isEnabled():
            return
        target = max(0, min(self._duration_ms or self.player.duration(), self.player.position() + delta_ms))
        self.player.setPosition(target)

    def _finish_seek(self) -> None:
        self._seeking = False
        self.player.setPosition(self.seek_slider.value())

    @Slot(int)
    def _on_position(self, pos_ms: int) -> None:
        if not self._seeking:
            self.seek_slider.setValue(pos_ms)
            self.time_current.setText(_format_duration(pos_ms / 1000.0))

    @Slot(int)
    def _on_duration(self, dur_ms: int) -> None:
        self._duration_ms = max(0, dur_ms)
        self.seek_slider.setRange(0, self._duration_ms)
        if dur_ms > 0:
            self.time_total.setText(_format_duration(dur_ms / 1000.0))

    @Slot(QMediaPlayer.PlaybackState)
    def _on_state_changed(self, state: QMediaPlayer.PlaybackState) -> None:
        if state == QMediaPlayer.PlaybackState.PlayingState:
            self.stage.stack.setCurrentIndex(1)
            self.play_btn.setIcon(lucide_icon("pause", "#F5F5F7", 16))
        else:
            self.play_btn.setIcon(lucide_icon("play", "#F5F5F7", 16))
            if state == QMediaPlayer.PlaybackState.StoppedState:
                self.stage.stack.setCurrentIndex(0)

    def toggle_mute(self) -> None:
        muted = not self.audio_output.isMuted()
        self.audio_output.setMuted(muted)
        self.mute_btn.setIcon(lucide_icon("volume-x" if muted else "volume-2", "#F5F5F7", 15))

    def _on_volume_changed(self, value: int) -> None:
        self.audio_output.setVolume(value / 100.0)
        if value > 0 and self.audio_output.isMuted():
            self.audio_output.setMuted(False)
            self.mute_btn.setIcon(lucide_icon("volume-2", "#F5F5F7", 15))

    def toggle_fullscreen(self) -> None:
        if not self.fullscreen_btn.isEnabled():
            return
        if self._fullscreen_dialog is not None:
            self._fullscreen_dialog.accept()
            return
        self.stage.stack.setCurrentIndex(1)
        dlg = _FullscreenVideoDialog(self.stage.video_widget, self.toggle_play, self)
        self._fullscreen_dialog = dlg
        dlg.showFullScreen()
        dlg.exec()
        self._fullscreen_dialog = None
        self.stage.stack.insertWidget(1, self.stage.video_widget)
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.stage.stack.setCurrentIndex(1)
        else:
            self.stage.stack.setCurrentIndex(0)
        self.stage._layout_stage()

    def _on_error(self, _error: QMediaPlayer.Error, message: str) -> None:
        self.desc_label.setText(f"Playback error: {message}")
