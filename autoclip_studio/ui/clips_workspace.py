from __future__ import annotations

import math
from pathlib import Path
from typing import Callable

import cv2
from PySide6.QtCore import QRectF, QSize, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QColor, QImage, QKeySequence, QPainter, QPainterPath, QResizeEvent, QShortcut
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
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

    def paintEvent(self, _event) -> None:
        t = theme_manager().tokens
        painter = QPainter(self)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        bounds = QRectF(self.rect())
        clip = QPainterPath()
        clip.addRoundedRect(bounds, 8, 8)
        painter.setClipPath(clip)
        painter.fillRect(bounds, QColor(t["bg_elevated"] if self._loading else t["bg_player"]))

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
            painter.setPen(QColor(t["text_secondary"]))
            painter.drawText(bounds, Qt.AlignmentFlag.AlignCenter, "…" if self._loading else "No thumb")

        if self._duration:
            font = painter.font()
            font.setPixelSize(10)
            font.setBold(True)
            painter.setFont(font)
            tw = painter.fontMetrics().horizontalAdvance(self._duration) + 8
            badge = QRectF(bounds.width() - tw - 4, bounds.height() - 20, tw, 16)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(16, 17, 22, 220))
            painter.drawRoundedRect(badge, 4, 4)
            painter.setPen(QColor("#F5F5F7"))
            painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, self._duration)


class ClipLibraryCard(QFrame):
    """Dynamic-height SaaS clip card supporting List View and Grid View with zero horizontal overflow."""

    clicked = Signal(str)

    def __init__(self, clip: dict, mode: str, status_text: str, *,
                 loading: bool = False, selected: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("clipLibraryCard")
        self.clip_id = str(clip["id"])
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(f"{clip['title']}\nFile: {Path(clip['path']).name}")
        self.setProperty("selected", "true" if selected else "false")

        t = theme_manager().tokens
        thumb_path = str(clip.get("thumbnail_path") or "")
        duration = float(clip.get("duration") or 0.0)
        score = int(clip.get("score") or 0)
        title = str(clip.get("title") or "Untitled clip")
        desc = str(clip.get("description") or "").strip()
        status_tone = "success" if status_text == "Ready" else ("danger" if status_text == "File missing" else "warning")

        if mode == "grid":
            self.setMinimumWidth(168)
            self.setMaximumWidth(230)
            self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
            root = QVBoxLayout(self)
            root.setContentsMargins(10, 10, 10, 10)
            root.setSpacing(8)
            thumb = _CroppedThumb(148, 220, self)
            thumb.set_data(thumb_path, duration, loading=loading)
            root.addWidget(thumb, alignment=Qt.AlignmentFlag.AlignCenter)

            title_lbl = QLabel(title)
            title_lbl.setWordWrap(True)
            title_lbl.setMinimumWidth(0)
            title_lbl.setStyleSheet(f"font-weight: 600; font-size: 13px; color: {t['text_primary']};")
            root.addWidget(title_lbl)

            badges = QHBoxLayout()
            badges.setSpacing(6)
            score_lbl = QLabel(f"★ {score}", objectName="scorePill")
            status_lbl = QLabel(status_text, objectName="statusChip")
            status_lbl.setProperty("tone", status_tone)
            badges.addWidget(score_lbl)
            badges.addStretch()
            badges.addWidget(status_lbl)
            root.addLayout(badges)
        else:
            self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
            self.setMinimumWidth(0)
            root = QHBoxLayout(self)
            root.setContentsMargins(12, 11, 12, 11)
            root.setSpacing(12)

            thumb = _CroppedThumb(56, 100, self)
            thumb.set_data(thumb_path, duration, loading=loading)
            root.addWidget(thumb, 0, Qt.AlignmentFlag.AlignTop)

            info = QVBoxLayout()
            info.setContentsMargins(0, 0, 0, 0)
            info.setSpacing(5)

            # Upper row: full-width naturally wrapping title on left, compact score + duration + status in upper-right!
            top_row = QHBoxLayout()
            top_row.setContentsMargins(0, 0, 0, 0)
            top_row.setSpacing(10)
            title_lbl = QLabel(title)
            title_lbl.setWordWrap(True)
            title_lbl.setMinimumWidth(0)
            title_lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
            title_lbl.setStyleSheet(f"font-weight: 600; font-size: 13.5px; color: {t['text_primary']};")

            meta_col = QVBoxLayout()
            meta_col.setContentsMargins(0, 0, 0, 0)
            meta_col.setSpacing(4)

            top_pills = QHBoxLayout()
            top_pills.setContentsMargins(0, 0, 0, 0)
            top_pills.setSpacing(4)
            top_pills.addStretch()
            score_lbl = QLabel(f"★ {score}", objectName="scorePill")
            dur_lbl = QLabel(_format_duration(duration), objectName="statusChip")
            for pill in (score_lbl, dur_lbl):
                pill.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
                top_pills.addWidget(pill)

            badge_text = "Pending" if status_text == "Thumbnail pending" else status_text
            status_lbl = QLabel(badge_text, objectName="statusChip")
            status_lbl.setProperty("tone", status_tone)
            status_lbl.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            if badge_text != status_text:
                status_lbl.setToolTip(status_text)

            meta_col.addLayout(top_pills)
            meta_col.addWidget(status_lbl, 0, Qt.AlignmentFlag.AlignRight)

            top_row.addWidget(title_lbl, 1)
            top_row.addLayout(meta_col)
            top_row.setAlignment(meta_col, Qt.AlignmentFlag.AlignTop)
            info.addLayout(top_row)

            if desc:
                preview_desc = desc if len(desc) <= 200 else (desc[:197].rstrip() + "…")
                desc_lbl = QLabel(preview_desc, objectName="muted")
                desc_lbl.setWordWrap(True)
                desc_lbl.setMinimumWidth(0)
                desc_lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
                info.addWidget(desc_lbl)

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
    """Master pane with summary metrics, search/sort/view toolbar, project filter, and vertical-only clip cards."""

    clip_selected = Signal(dict)
    export_all_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumWidth(280)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._all_clips: list[dict] = []
        self._filtered_clips: list[dict] = []
        self._busy_ids: set[str] = set()
        self._selected_id: str | None = None
        self._project_filter_id: str | None = None
        self._project_filter_title: str = ""
        self._view_mode = "list"
        self._cards: dict[str, ClipLibraryCard] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)

        # Compact summary strip
        summary_row = QHBoxLayout()
        summary_row.setSpacing(8)
        self.summary_count = self._summary_pill("Total clips", "0")
        self.summary_duration = self._summary_pill("Duration", "00:00")
        self.summary_score = self._summary_pill("Avg score", "—")
        for pill in (self.summary_count, self.summary_duration, self.summary_score):
            summary_row.addWidget(pill, 1)
        root.addLayout(summary_row)

        # Optional active project filter chip (triggered from Job Completed popup / queue row)
        self.project_filter_bar = QFrame(objectName="summaryPill")
        pf_lay = QHBoxLayout(self.project_filter_bar)
        pf_lay.setContentsMargins(10, 6, 10, 6)
        pf_lay.setSpacing(8)
        self.project_filter_label = QLabel("", objectName="muted")
        self.project_filter_label.setWordWrap(True)
        self.project_filter_clear_btn = QPushButton("Show All Clips")
        self.project_filter_clear_btn.clicked.connect(self.clear_project_filter)
        pf_lay.addWidget(self.project_filter_label, 1)
        pf_lay.addWidget(self.project_filter_clear_btn)
        self.project_filter_bar.hide()
        root.addWidget(self.project_filter_bar)

        # Functional toolbar
        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search clips…")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.setAccessibleName("Search generated clips")
        self._search_action = self.search_input.addAction(
            lucide_icon("search", theme_manager().tokens["text_secondary"], 14),
            QLineEdit.ActionPosition.LeadingPosition,
        )
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

        # Independently scrollable vertical-only library (ZERO horizontal scrollbar!)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._container = QWidget()
        self._container.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Minimum)
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
        t = theme_manager().tokens
        fg = t["text_primary"]
        self.list_view_btn.setIcon(lucide_icon("list", fg, 15))
        self.grid_view_btn.setIcon(lucide_icon("grid", fg, 15))
        self.export_all_btn.setIcon(lucide_icon("download", fg, 15))
        if hasattr(self, "_search_action"):
            self._search_action.setIcon(lucide_icon("search", t["text_secondary"], 14))
        if self._all_clips:
            self._rebuild_cards()

    def filter_by_project(self, project_id: str, project_title: str = "") -> None:
        self._project_filter_id = str(project_id) if project_id else None
        self._project_filter_title = project_title or project_id
        if self._project_filter_id:
            self.project_filter_label.setText(f"Showing clips from: {self._project_filter_title}")
            self.project_filter_bar.show()
        else:
            self.project_filter_bar.hide()
        self._rebuild_cards()

    def clear_project_filter(self) -> None:
        self._project_filter_id = None
        self._project_filter_title = ""
        self.project_filter_bar.hide()
        self._rebuild_cards()

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
        source = [
            c for c in self._all_clips
            if not self._project_filter_id or str(c.get("project_id") or "") == self._project_filter_id
        ]
        total = len(source)
        total_dur = sum(float(c.get("duration") or 0.0) for c in source)
        avg_score = round(sum(int(c.get("score") or 0) for c in source) / total) if total else 0
        self.summary_count.findChild(QLabel, "summaryValue").setText(str(total))
        self.summary_duration.findChild(QLabel, "summaryValue").setText(_format_duration(total_dur))
        self.summary_score.findChild(QLabel, "summaryValue").setText(f"{avg_score}/100" if total else "—")
        self.export_all_btn.setEnabled(len(self._all_clips) > 0)

    def _sorted_filtered(self) -> list[dict]:
        query = self.search_input.text().strip().lower()
        items = [
            c for c in self._all_clips
            if (not self._project_filter_id or str(c.get("project_id") or "") == self._project_filter_id)
            and (
                not query
                or query in str(c.get("title", "")).lower()
                or query in str(c.get("description", "")).lower()
            )
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
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
            elif item.layout():
                self._clear_sublayout(item.layout())

    @staticmethod
    def _clear_sublayout(lay) -> None:
        while lay.count():
            child = lay.takeAt(0)
            widget = child.widget()
            if widget is not None:
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
            elif child.layout():
                ClipLibraryPanel._clear_sublayout(child.layout())

    def _rebuild_cards(self) -> None:
        self._clear_container()
        self._update_summary()
        self._filtered_clips = self._sorted_filtered()

        if not self._filtered_clips:
            empty = QFrame(objectName="card")
            elay = QVBoxLayout(empty)
            elay.setContentsMargins(24, 32, 24, 32)
            elay.setSpacing(6)
            msg = "No clips match your filter." if self._all_clips else "No generated clips yet."
            lbl = QLabel(msg, objectName="sectionTitle")
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            sub = QLabel("Create a project to generate short-form vertical clips.", objectName="muted")
            sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
            sub.setWordWrap(True)
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
            cols = max(1, (self.scroll.viewport().width() - 8) // 184)
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
            for col_i in range(cols):
                grid.setColumnStretch(col_i, 1)
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

        self._container_layout.addStretch(1)
        self._container.adjustSize()
        self._container.updateGeometry()
        current = self.selected_clip()
        if current:
            self.clip_selected.emit(current)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if self._view_mode == "grid" and self._filtered_clips:
            self._rebuild_cards()

    def select_clip(self, clip_id: str) -> None:
        self.select_clip_by_id(clip_id)

    def select_clip_by_id(self, clip_id: str) -> None:
        self._selected_id = clip_id
        for cid, card in self._cards.items():
            card.set_selected(cid == clip_id)
        clip = self.selected_clip()
        if clip:
            self.clip_selected.emit(clip)

    def select_relative(self, delta: int) -> None:
        if not self._filtered_clips:
            return
        ids = [str(c["id"]) for c in self._filtered_clips]
        try:
            idx = ids.index(self._selected_id or "")
        except ValueError:
            idx = 0
        target = (idx + delta) % len(ids)
        self.select_clip_by_id(ids[target])

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
    """Centered 9:16 video/thumbnail stage with intentional dark viewport and zero distortion."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("playerStage")
        self.setMinimumSize(200, 240)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
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
        pad = 6
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


class _FullscreenPlayerWindow(QWidget):
    """Dedicated native fullscreen Qt Multimedia window sharing the single QMediaPlayer cleanly."""

    exited = Signal()

    def __init__(
        self,
        player: QMediaPlayer,
        audio_output: QAudioOutput,
        on_toggle_play: Callable[[], None],
        on_toggle_mute: Callable[[], None],
        on_volume: Callable[[int], None],
        on_seek: Callable[[int], None],
    ) -> None:
        super().__init__(None, Qt.WindowType.Window)
        self.setWindowTitle("AutoClip Studio — Fullscreen Preview")
        self.setStyleSheet("background-color: #08090C; color: #F5F5F7;")
        self._player = player
        self._audio = audio_output
        self._on_toggle_play = on_toggle_play
        self._on_seek = on_seek
        self._seeking = False
        self._closed = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.video_widget = QVideoWidget(self)
        self.video_widget.setAspectRatioMode(Qt.AspectRatioMode.KeepAspectRatio)
        self.video_widget.setStyleSheet("background-color: #08090C;")
        layout.addWidget(self.video_widget, 1)

        # Clean bottom control bar (never overlapping video frames!)
        bar = QFrame()
        bar.setStyleSheet("background-color: #12141A; border-top: 1px solid #262933;")
        bar_lay = QVBoxLayout(bar)
        bar_lay.setContentsMargins(18, 10, 18, 12)
        bar_lay.setSpacing(8)

        tl = QHBoxLayout()
        tl.setSpacing(10)
        self.time_current = QLabel("00:00")
        self.time_current.setStyleSheet("color: #A0A5B2; font-size: 12px;")
        self.seek_slider = QSlider(Qt.Orientation.Horizontal)
        self.seek_slider.setRange(0, max(0, player.duration()))
        self.seek_slider.setValue(max(0, player.position()))
        self.seek_slider.sliderPressed.connect(lambda: setattr(self, "_seeking", True))
        self.seek_slider.sliderReleased.connect(self._finish_seek)
        self.seek_slider.sliderMoved.connect(lambda pos: self.time_current.setText(_format_duration(pos / 1000.0)))
        self.time_total = QLabel(_format_duration(player.duration() / 1000.0))
        self.time_total.setStyleSheet("color: #A0A5B2; font-size: 12px;")
        tl.addWidget(self.time_current)
        tl.addWidget(self.seek_slider, 1)
        tl.addWidget(self.time_total)
        bar_lay.addLayout(tl)

        ctrl = QHBoxLayout()
        ctrl.setSpacing(10)
        self.play_btn = QPushButton()
        self.play_btn.setFixedSize(36, 32)
        self.play_btn.clicked.connect(on_toggle_play)
        self.mute_btn = QPushButton()
        self.mute_btn.setFixedSize(36, 32)
        self.mute_btn.clicked.connect(on_toggle_mute)
        self.vol_slider = QSlider(Qt.Orientation.Horizontal)
        self.vol_slider.setRange(0, 100)
        self.vol_slider.setValue(int(round(audio_output.volume() * 100)))
        self.vol_slider.setFixedWidth(96)
        self.vol_slider.valueChanged.connect(on_volume)
        self.exit_btn = QPushButton("Exit Fullscreen")
        self.exit_btn.setIcon(lucide_icon("minimize", "#F5F5F7", 15))
        self.exit_btn.clicked.connect(self.close)
        ctrl.addWidget(self.play_btn)
        ctrl.addStretch()
        ctrl.addWidget(self.mute_btn)
        ctrl.addWidget(self.vol_slider)
        ctrl.addWidget(self.exit_btn)
        bar_lay.addLayout(ctrl)
        layout.addWidget(bar)

        self.sync_icons()

        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, activated=self.close)
        QShortcut(QKeySequence(Qt.Key.Key_F), self, activated=self.close)
        QShortcut(QKeySequence(Qt.Key.Key_Space), self, activated=on_toggle_play)

    def sync_icons(self) -> None:
        playing = self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        self.play_btn.setIcon(lucide_icon("pause" if playing else "play", "#F5F5F7", 16))
        self.mute_btn.setIcon(lucide_icon("volume-x" if self._audio.isMuted() else "volume-2", "#F5F5F7", 15))

    def sync_position(self, pos_ms: int) -> None:
        if not self._seeking:
            self.seek_slider.setValue(pos_ms)
            self.time_current.setText(_format_duration(pos_ms / 1000.0))

    def sync_duration(self, dur_ms: int) -> None:
        self.seek_slider.setRange(0, max(0, dur_ms))
        self.time_total.setText(_format_duration(dur_ms / 1000.0))

    def _finish_seek(self) -> None:
        self._seeking = False
        self._on_seek(self.seek_slider.value())

    def closeEvent(self, event) -> None:
        if not self._closed:
            self._closed = True
            self.exited.emit()
        super().closeEvent(event)


class ClipDetailPlayerPanel(QFrame):
    """Right-side native 9:16 video player, theme-safe transport, structured details, and sticky footer."""

    open_file_requested = Signal()
    open_folder_requested = Signal()
    regenerate_thumb_requested = Signal()
    select_frame_requested = Signal()
    export_thumb_requested = Signal()
    rename_clip_requested = Signal()
    delete_clip_requested = Signal()
    prev_clip_requested = Signal()
    next_clip_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("card")
        self.setMinimumWidth(320)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._current_clip: dict | None = None
        self._seeking = False
        self._duration_ms = 0
        self._fullscreen_win: _FullscreenPlayerWindow | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        # 1. Video preview (centered 9:16 dark viewport, cleanly separated from controls)
        self.stage = AspectVideoStage(self)
        root.addWidget(self.stage, 3)

        # Single shared Qt Multimedia player
        self.audio_output = QAudioOutput(self)
        self.audio_output.setVolume(0.8)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio_output)
        self.player.setVideoOutput(self.stage.video_widget)
        self.player.positionChanged.connect(self._on_position)
        self.player.durationChanged.connect(self._on_duration)
        self.player.playbackStateChanged.connect(self._on_state_changed)
        self.player.mediaStatusChanged.connect(self._on_media_status)
        self.player.errorOccurred.connect(self._on_error)

        # 2. Playback controls (below viewport, never overlapping captions)
        controls_frame = QFrame(objectName="summaryPill")
        ctrl_lay = QVBoxLayout(controls_frame)
        ctrl_lay.setContentsMargins(10, 8, 10, 8)
        ctrl_lay.setSpacing(8)

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
        ctrl_lay.addLayout(timeline)

        transport = QHBoxLayout()
        transport.setSpacing(4)
        self.prev_clip_btn = QPushButton(objectName="iconButton")
        self.prev_clip_btn.setToolTip("Previous clip")
        self.prev_clip_btn.setAccessibleName("Previous clip")
        self.prev_clip_btn.clicked.connect(self.prev_clip_requested.emit)

        self.skip_back_btn = QPushButton(objectName="iconButton")
        self.skip_back_btn.setToolTip("Back 5s (Left / J)")
        self.skip_back_btn.setAccessibleName("Seek back 5 seconds")
        self.skip_back_btn.clicked.connect(lambda: self.seek_relative(-5000))

        self.play_btn = QPushButton(objectName="iconButton")
        self.play_btn.setToolTip("Play / Pause (Space / K)")
        self.play_btn.setAccessibleName("Play or pause")
        self.play_btn.clicked.connect(self.toggle_play)

        self.skip_fwd_btn = QPushButton(objectName="iconButton")
        self.skip_fwd_btn.setToolTip("Forward 5s (Right / L)")
        self.skip_fwd_btn.setAccessibleName("Seek forward 5 seconds")
        self.skip_fwd_btn.clicked.connect(lambda: self.seek_relative(5000))

        self.next_clip_btn = QPushButton(objectName="iconButton")
        self.next_clip_btn.setToolTip("Next clip")
        self.next_clip_btn.setAccessibleName("Next clip")
        self.next_clip_btn.clicked.connect(self.next_clip_requested.emit)

        self.mute_btn = QPushButton(objectName="iconButton")
        self.mute_btn.setToolTip("Mute / Unmute (M)")
        self.mute_btn.setAccessibleName("Mute or unmute")
        self.mute_btn.clicked.connect(self.toggle_mute)

        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(80)
        self.volume_slider.setMinimumWidth(44)
        self.volume_slider.setMaximumWidth(74)
        self.volume_slider.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.volume_slider.setAccessibleName("Volume")
        self.volume_slider.valueChanged.connect(self._on_volume_changed)

        self.fullscreen_btn = QPushButton(objectName="iconButton")
        self.fullscreen_btn.setToolTip("Fullscreen (F)")
        self.fullscreen_btn.setAccessibleName("Fullscreen playback")
        self.fullscreen_btn.clicked.connect(self.toggle_fullscreen)

        for b in (
            self.prev_clip_btn, self.skip_back_btn, self.play_btn, self.skip_fwd_btn,
            self.next_clip_btn, self.mute_btn, self.fullscreen_btn,
        ):
            b.setFixedSize(28, 28)
            b.setEnabled(False)

        transport.addWidget(self.prev_clip_btn)
        transport.addWidget(self.skip_back_btn)
        transport.addWidget(self.play_btn)
        transport.addWidget(self.skip_fwd_btn)
        transport.addWidget(self.next_clip_btn)
        transport.addStretch()
        transport.addWidget(self.mute_btn)
        transport.addWidget(self.volume_slider)
        transport.addWidget(self.fullscreen_btn)
        ctrl_lay.addLayout(transport)
        root.addWidget(controls_frame)

        # 3-6. Vertically scrollable details section (full-width title, score breakdown, description, reason)
        self.detail_scroll = QScrollArea()
        self.detail_scroll.setWidgetResizable(True)
        self.detail_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.detail_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        detail_host = QWidget()
        info_box = QVBoxLayout(detail_host)
        info_box.setContentsMargins(0, 2, 4, 2)
        info_box.setSpacing(10)

        # 3. Clip title & metadata (full width, natural wrap — NO oversized badge beside title!)
        self.title_label = QLabel("Select a clip", objectName="sectionTitle")
        self.title_label.setWordWrap(True)
        self.title_label.setMinimumWidth(0)
        self.specs_label = QLabel("—", objectName="muted")
        self.specs_label.setWordWrap(True)
        self.specs_label.setMinimumWidth(0)
        info_box.addWidget(self.title_label)
        info_box.addWidget(self.specs_label)

        # 4. Dedicated compact Score Breakdown section
        score_card = QFrame(objectName="summaryPill")
        sc_lay = QVBoxLayout(score_card)
        sc_lay.setContentsMargins(10, 8, 10, 8)
        sc_lay.setSpacing(6)

        sc_head = QHBoxLayout()
        sc_head.setSpacing(8)
        sc_head.addWidget(QLabel("Score Breakdown", objectName="muted"))
        sc_head.addStretch()
        self.score_badge = QLabel("—", objectName="scorePill")
        sc_head.addWidget(self.score_badge)
        sc_lay.addLayout(sc_head)

        self.score_bar = QProgressBar()
        self.score_bar.setRange(0, 100)
        self.score_bar.setValue(0)
        self.score_bar.setFixedHeight(5)
        self.score_bar.setTextVisible(False)
        sc_lay.addWidget(self.score_bar)

        score_breakdown = QHBoxLayout()
        score_breakdown.setSpacing(6)
        self.ai_score_chip = QLabel("AI: —", objectName="statusChip")
        self.replay_score_chip = QLabel("Replay: —", objectName="statusChip")
        self.mode_chip = QLabel("Mode: —", objectName="statusChip")
        for chip in (self.ai_score_chip, self.replay_score_chip, self.mode_chip):
            score_breakdown.addWidget(chip)
        score_breakdown.addStretch()
        sc_lay.addLayout(score_breakdown)
        info_box.addWidget(score_card)

        # 5. Description
        desc_hdr = QLabel("Description", objectName="helperText")
        desc_hdr.setStyleSheet("font-weight: 600; text-transform: uppercase;")
        self.desc_label = QLabel("Select a clip from the library to inspect and play.", objectName="muted")
        self.desc_label.setWordWrap(True)
        self.desc_label.setMinimumWidth(0)
        info_box.addWidget(desc_hdr)
        info_box.addWidget(self.desc_label)

        # 6. Why this clip was selected
        self.reason_hdr = QLabel("Why this clip was selected", objectName="helperText")
        self.reason_hdr.setStyleSheet("font-weight: 600; text-transform: uppercase;")
        self.reason_label = QLabel("", objectName="muted")
        self.reason_label.setWordWrap(True)
        self.reason_label.setMinimumWidth(0)
        self.reason_hdr.hide()
        self.reason_label.hide()
        info_box.addWidget(self.reason_hdr)
        info_box.addWidget(self.reason_label)
        info_box.addStretch()

        self.detail_scroll.setWidget(detail_host)
        root.addWidget(self.detail_scroll, 2)

        # 7. Separated sticky footer actions
        divider = QFrame(objectName="divider")
        root.addWidget(divider)

        actions_row = QHBoxLayout()
        actions_row.setSpacing(8)
        self.primary_btn = QPushButton("Open File", objectName="primary")
        self.primary_btn.clicked.connect(self.open_file_requested.emit)
        self.primary_btn.setEnabled(False)

        self.more_btn = QPushButton(objectName="iconButton")
        self.more_btn.setText(" Actions")
        self.more_btn.setAccessibleName("Clip actions menu")
        self.more_btn.setEnabled(False)

        self.actions_menu = QMenu(self)
        self.action_open_file = self.actions_menu.addAction("Open File", self.open_file_requested.emit)
        self.action_open_folder = self.actions_menu.addAction("Open Containing Folder", self.open_folder_requested.emit)
        self.actions_menu.addSeparator()
        self.action_regen_thumb = self.actions_menu.addAction("Regenerate Thumbnail", self.regenerate_thumb_requested.emit)
        self.action_pick_frame = self.actions_menu.addAction("Select Thumbnail Frame…", self.select_frame_requested.emit)
        self.action_export_thumb = self.actions_menu.addAction("Export Thumbnail…", self.export_thumb_requested.emit)
        self.action_rename_clip = self.actions_menu.addAction("Rename Clip…", self.rename_clip_requested.emit)
        self.actions_menu.addSeparator()
        self.action_delete_clip = self.actions_menu.addAction("Delete Clip", self.delete_clip_requested.emit)
        self.more_btn.setMenu(self.actions_menu)

        actions_row.addWidget(self.primary_btn, 1)
        actions_row.addWidget(self.more_btn)
        root.addLayout(actions_row)

        self.apply_theme()

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

    def _icon_fg(self) -> str:
        return theme_manager().tokens["text_primary"]

    def _sync_play_icon(self) -> None:
        playing = self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        self.play_btn.setIcon(lucide_icon("pause" if playing else "play", self._icon_fg(), 15))
        if self._fullscreen_win is not None:
            self._fullscreen_win.sync_icons()

    def _sync_mute_icon(self) -> None:
        muted = self.audio_output.isMuted()
        self.mute_btn.setIcon(lucide_icon("volume-x" if muted else "volume-2", self._icon_fg(), 15))
        if self._fullscreen_win is not None:
            self._fullscreen_win.sync_icons()

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
            self.replay_score_chip.hide()
            self.mode_chip.setText("Mode: —")
            self.specs_label.setText("—")
            self.desc_label.setText("Select a clip from the library to inspect and play.")
            self.reason_hdr.hide()
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
        if replay_score is not None:
            self.replay_score_chip.setText(f"Replay: {int(replay_score)}/100")
            self.replay_score_chip.show()
        else:
            self.replay_score_chip.hide()
        self.mode_chip.setText(f"Mode: {mode_names.get(mode_raw, mode_raw)}")
        reason_text = str(self._current_clip.get("reason") or "").strip()
        if reason_text:
            self.reason_label.setText(reason_text)
            self.reason_hdr.show()
            self.reason_label.show()
        else:
            self.reason_hdr.hide()
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
        for b in (
            self.skip_back_btn, self.play_btn, self.skip_fwd_btn,
            self.mute_btn, self.fullscreen_btn, self.primary_btn,
        ):
            b.setEnabled(has_clip and file_exists)
        self.prev_clip_btn.setEnabled(has_clip)
        self.next_clip_btn.setEnabled(has_clip)
        self.more_btn.setEnabled(has_clip)
        for act in (self.action_open_file, self.action_open_folder, self.action_regen_thumb,
                    self.action_pick_frame, self.action_export_thumb):
            act.setEnabled(has_clip and file_exists)
        self.action_rename_clip.setEnabled(has_clip)
        self.action_delete_clip.setEnabled(has_clip)

    def apply_theme(self, _resolved: str = "") -> None:
        t = theme_manager().tokens
        fg = t["text_primary"]
        danger = t["semantic_danger_fg"]
        self.prev_clip_btn.setIcon(lucide_icon("chevron-left", fg, 15))
        self.skip_back_btn.setIcon(lucide_icon("skip-back", fg, 14))
        self._sync_play_icon()
        self.skip_fwd_btn.setIcon(lucide_icon("skip-forward", fg, 14))
        self.next_clip_btn.setIcon(lucide_icon("chevron-right", fg, 15))
        self._sync_mute_icon()
        self.fullscreen_btn.setIcon(lucide_icon("maximize", fg, 14))
        self.more_btn.setIcon(lucide_icon("more-horizontal", fg, 15))
        self.primary_btn.setIcon(lucide_icon("external-link", "#FFFFFF", 15))
        # Menu action icons (theme-aware!)
        self.action_open_file.setIcon(lucide_icon("external-link", fg, 14))
        self.action_open_folder.setIcon(lucide_icon("folder-open", fg, 14))
        self.action_regen_thumb.setIcon(lucide_icon("refresh-cw", fg, 14))
        self.action_pick_frame.setIcon(lucide_icon("image", fg, 14))
        self.action_export_thumb.setIcon(lucide_icon("download", fg, 14))
        self.action_rename_clip.setIcon(lucide_icon("edit-3", fg, 14))
        self.action_delete_clip.setIcon(lucide_icon("trash-2", danger, 14))

    def stop_playback(self) -> None:
        if self._fullscreen_win is not None:
            self._exit_fullscreen()
        if self.player.playbackState() != QMediaPlayer.PlaybackState.StoppedState:
            self.player.stop()
        self.stage.stack.setCurrentIndex(0)
        self._sync_play_icon()

    def toggle_play(self) -> None:
        if not self.play_btn.isEnabled():
            return
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            if self.player.mediaStatus() == QMediaPlayer.MediaStatus.EndOfMedia:
                self.player.setPosition(0)
            if self._fullscreen_win is None:
                self.stage.stack.setCurrentIndex(1)
            self.player.play()
        self._sync_play_icon()

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
        if self._fullscreen_win is not None:
            self._fullscreen_win.sync_position(pos_ms)

    @Slot(int)
    def _on_duration(self, dur_ms: int) -> None:
        self._duration_ms = max(0, dur_ms)
        self.seek_slider.setRange(0, self._duration_ms)
        if dur_ms > 0:
            self.time_total.setText(_format_duration(dur_ms / 1000.0))
        if self._fullscreen_win is not None:
            self._fullscreen_win.sync_duration(self._duration_ms)

    @Slot(QMediaPlayer.PlaybackState)
    def _on_state_changed(self, state: QMediaPlayer.PlaybackState) -> None:
        if state == QMediaPlayer.PlaybackState.PlayingState:
            if self._fullscreen_win is None:
                self.stage.stack.setCurrentIndex(1)
        elif state == QMediaPlayer.PlaybackState.StoppedState and self._fullscreen_win is None:
            self.stage.stack.setCurrentIndex(0)
        self._sync_play_icon()

    @Slot(QMediaPlayer.MediaStatus)
    def _on_media_status(self, status: QMediaPlayer.MediaStatus) -> None:
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            self._sync_play_icon()

    def toggle_mute(self) -> None:
        muted = not self.audio_output.isMuted()
        self.audio_output.setMuted(muted)
        self._sync_mute_icon()

    def _on_volume_changed(self, value: int) -> None:
        self.audio_output.setVolume(value / 100.0)
        if value > 0 and self.audio_output.isMuted():
            self.audio_output.setMuted(False)
        self._sync_mute_icon()

    def toggle_fullscreen(self) -> None:
        if not self.fullscreen_btn.isEnabled():
            return
        if self._fullscreen_win is not None:
            self._exit_fullscreen()
            return
        # Seamless video output handoff to dedicated fullscreen QVideoWidget (preserves position, state, volume, mute!)
        was_playing = self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        pos = self.player.position()
        fs = _FullscreenPlayerWindow(
            self.player,
            self.audio_output,
            self.toggle_play,
            self.toggle_mute,
            lambda val: self.volume_slider.setValue(val),
            lambda pos_ms: self.player.setPosition(pos_ms),
        )
        fs.exited.connect(self._exit_fullscreen)
        self._fullscreen_win = fs
        self.player.setVideoOutput(fs.video_widget)
        fs.showFullScreen()
        fs.raise_()
        fs.activateWindow()
        if pos > 0:
            self.player.setPosition(pos)
        if was_playing and self.player.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
            self.player.play()
        fs.sync_icons()

    def _exit_fullscreen(self) -> None:
        fs = self._fullscreen_win
        if fs is None:
            return
        self._fullscreen_win = None
        was_playing = self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        pos = self.player.position()
        self.player.setVideoOutput(self.stage.video_widget)
        if not fs._closed:
            fs._closed = True
            fs.close()
        fs.deleteLater()
        self.stage.stack.setCurrentIndex(1 if (was_playing or pos > 0) else 0)
        self.stage._layout_stage()
        if was_playing and self.player.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
            self.player.play()
        self._sync_play_icon()

    def _on_error(self, _error: QMediaPlayer.Error, message: str) -> None:
        self.desc_label.setText(f"Playback error: {message}")
