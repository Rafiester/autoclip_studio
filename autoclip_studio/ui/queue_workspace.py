"""Modern SaaS Processing Queue workspace components for AutoClip Studio."""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QFrame, QGridLayout,
    QHBoxLayout, QLabel, QLineEdit, QMenu, QPlainTextEdit, QProgressBar, QPushButton,
    QScrollArea, QSizePolicy, QStackedWidget, QVBoxLayout, QWidget,
)

from ..diagnostics import redact
from ..media import is_youtube_url
from .download_view import DownloadView
from .icons import lucide_icon
from .log_view import LogView
from .speech_view import SpeechProgressView
from .theme import theme_manager

QUEUE_STATUS_META: dict[str, tuple[str, str, str, str]] = {
    "processing": ("Processing", "#E53935", "rgba(229, 57, 53, 0.16)", "#FF8A80"),
    "queued": ("Queued", "#F5A623", "rgba(245, 166, 35, 0.14)", "#FFD07D"),
    "completed": ("Completed", "#2ECC71", "rgba(46, 204, 113, 0.14)", "#7BE3A6"),
    "failed": ("Failed", "#FF6B7A", "rgba(255, 107, 122, 0.14)", "#FF9EA8"),
    "analysis_failed": ("Failed", "#FF6B7A", "rgba(255, 107, 122, 0.14)", "#FF9EA8"),
    "cancelled": ("Cancelled", "#A3A8B5", "rgba(163, 168, 181, 0.12)", "#BFC4D0"),
}


def _status_chip_style(status: str) -> tuple[str, str]:
    """Return (label, tone) for semantic QSS statusChip styling in Light and Dark themes."""
    mapping = {
        "processing": ("Processing", "info"),
        "queued": ("Queued", "warning"),
        "completed": ("Completed", "success"),
        "failed": ("Failed", "danger"),
        "analysis_failed": ("Failed", "danger"),
        "cancelled": ("Cancelled", ""),
    }
    return mapping.get(status, ("Queued", ""))


def _stage_palette(state: str) -> tuple[str, str, str, str]:
    """Return theme-aware (dot_accent, sub_fg, chip_bg, border_color) for PipelineStepperWidget."""
    t = theme_manager().tokens
    if state == "completed":
        return (t["semantic_success"], t["semantic_success"], t["semantic_success_bg"], t["border_subtle"])
    if state == "running":
        return (t["brand_primary"], t["brand_accent"], t["brand_tint"], t["brand_primary"])
    if state == "waiting":
        return (t["semantic_warning"], t["semantic_warning"], t["semantic_warning_bg"], t["semantic_warning"])
    if state == "failed":
        return (t["semantic_danger_fg"], t["semantic_danger_fg"], t["semantic_danger_bg"], t["semantic_danger_border"])
    if state == "cancelled":
        return (t["text_secondary"], t["text_secondary"], t["bg_elevated"], t["border_subtle"])
    if state == "skipped":
        return (t["text_disabled"], t["text_disabled"], t["bg_alt_row"], t["border_subtle"])
    return (t["text_disabled"], t["text_secondary"], t["bg_elevated"], t["border_subtle"])


def parse_project_options(project: dict[str, Any] | None) -> dict[str, Any]:
    """Safely parse the persisted JSON options dictionary for a project."""
    if not project:
        return {}
    raw = project.get("options")
    if isinstance(raw, dict):
        return dict(raw)
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def project_display_name(project: dict[str, Any] | None) -> str:
    """Return a clean human-friendly project title from persisted metadata."""
    if not project:
        return "Untitled Project"
    options = parse_project_options(project)
    name = str(options.get("project_name") or "").strip()
    if name:
        return name
    yt_meta = options.get("youtube_metadata")
    if isinstance(yt_meta, dict):
        title = str(yt_meta.get("title") or "").strip()
        if title:
            return title
    source = str(project.get("source") or "").strip()
    if is_youtube_url(source):
        return f"YouTube Video ({source[-11:]})" if len(source) >= 11 else "YouTube Video"
    if source:
        return Path(source).stem or Path(source).name
    return "Untitled Project"


def project_source_info(project: dict[str, Any] | None) -> tuple[str, str]:
    """Return (source_type_label, concise_source_detail) for a project."""
    if not project:
        return ("Local File", "")
    source = str(project.get("source") or "").strip()
    if is_youtube_url(source):
        return ("YouTube", source)
    path = Path(source) if source else None
    return ("Local File", path.name if path and path.name else source)


def _format_mmss(seconds: float | int | None) -> str:
    if not seconds or seconds <= 0:
        return ""
    total = int(round(float(seconds)))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def project_media_summary(project: dict[str, Any] | None) -> str:
    """Return concise media specs like '1080p • 9:16 • 04:12' from saved options/metadata."""
    if not project:
        return ""
    options = parse_project_options(project)
    parts: list[str] = []
    resolution = str(options.get("resolution") or "").strip()
    aspect = str(options.get("aspect_ratio") or "").strip()
    if resolution:
        parts.append(resolution)
    if aspect:
        parts.append(aspect)
    yt_meta = options.get("youtube_metadata")
    if isinstance(yt_meta, dict):
        duration_text = str(yt_meta.get("duration_text") or "").strip()
        duration_secs = yt_meta.get("duration_seconds")
        if duration_text:
            parts.append(duration_text)
        elif duration_secs:
            formatted = _format_mmss(duration_secs)
            if formatted:
                parts.append(formatted)
    if options.get("srt_path"):
        parts.append("SRT")
    return " • ".join(parts) if parts else "Standard preset"


def project_thumbnail_path(project: dict[str, Any] | None, clips: list[dict[str, Any]] | None = None) -> str:
    """Return an existing thumbnail image path for the project when available."""
    if not project:
        return ""
    pid = str(project.get("id") or "")
    for clip in clips or ():
        if str(clip.get("project_id") or "") == pid:
            for key in ("thumbnail_path", "thumbnail_raw_path"):
                candidate = str(clip.get(key) or "").strip()
                if candidate and Path(candidate).is_file():
                    return candidate
    options = parse_project_options(project)
    yt_meta = options.get("youtube_metadata")
    if isinstance(yt_meta, dict):
        for key in ("thumbnail_path", "local_thumbnail"):
            candidate = str(yt_meta.get(key) or "").strip()
            if candidate and Path(candidate).is_file():
                return candidate
    return ""


def format_timestamp_short(iso_text: str | None) -> str:
    """Format an ISO timestamp for compact row cards."""
    if not iso_text:
        return "—"
    try:
        dt = datetime.fromisoformat(iso_text)
        return dt.strftime("%b %d, %H:%M")
    except ValueError:
        return str(iso_text)[:16]


def format_elapsed(seconds: float) -> str:
    """Format elapsed wall-clock seconds into MM:SS or H:MM:SS."""
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def estimate_eta_text(started_at: float | None, progress: int, *, status: str = "processing") -> str:
    """Compute measurable ETA only once progress and elapsed time are meaningful."""
    if status != "processing" or started_at is None or progress < 5 or progress >= 100:
        return "—"
    elapsed = max(0.0, time.monotonic() - started_at)
    if elapsed < 3.0:
        return "—"
    remaining = elapsed * (100.0 - progress) / float(progress)
    return format_elapsed(remaining)


def compute_pipeline_stages(
    options: dict[str, Any],
    status: str,
    progress: int,
    stage_message: str = "",
) -> list[dict[str, str]]:
    """Compute the 6-stage visual stepper state dynamically from job options and progress."""
    source = str(options.get("source") or "")
    is_yt = is_youtube_url(source) if source else True
    has_srt = bool(str(options.get("srt_path") or "").strip())
    use_face = bool(options.get("face_tracking", True))
    use_captions = bool(options.get("captions", True))

    stage_defs = [
        ("ingest", "Download" if is_yt else "Local Import", "YouTube" if is_yt else "Skipped"),
        ("speech", "SRT Import" if has_srt else "Transcription", "Skipped" if has_srt else "Audio"),
        ("analysis", "AI Highlights", "Scoring"),
        ("tracking", "Face Tracking", "Skipped" if not use_face else "Reframe"),
        ("captions", "Captions", "Skipped" if not use_captions else "Styled"),
        ("export", "Export & Thumbs", "MP4 + PNG"),
    ]

    msg_lower = stage_message.lower()
    is_waiting = (
        status == "processing"
        and any(tok in msg_lower for tok in ("waiting", "gaming mode", "thermal", "cooling", "throttl"))
    )

    if status == "completed" or progress >= 100:
        out = []
        for idx, (key, title, sub) in enumerate(stage_defs):
            if (idx == 0 and not is_yt) or (idx == 1 and has_srt) or (idx == 3 and not use_face) or (idx == 4 and not use_captions):
                out.append({"key": key, "title": title, "subtitle": sub, "state": "skipped"})
            else:
                out.append({"key": key, "title": title, "subtitle": "Done", "state": "completed"})
        return out

    # Determine active stage index from progress percentage + stage hints
    if status == "analysis_failed":
        active_idx = 2
    elif progress < 12:
        active_idx = 0
    elif progress < 40:
        active_idx = 1
    elif progress < 48:
        active_idx = 2
    elif "thumbnail" in msg_lower or progress >= 82:
        active_idx = 5
    elif "caption" in msg_lower or (64 <= progress < 82):
        active_idx = 4
    else:
        active_idx = 3

    # Skip disabled stages cleanly if active pointer lands on one
    skipped_flags = {
        0: not is_yt,
        1: has_srt,
        3: not use_face,
        4: not use_captions,
    }

    results: list[dict[str, str]] = []
    for idx, (key, title, default_sub) in enumerate(stage_defs):
        is_skipped = skipped_flags.get(idx, False)
        if status == "queued" or (status == "processing" and progress <= 0):
            state = "skipped" if is_skipped else "pending"
            sub = default_sub if is_skipped else "Waiting"
        elif idx < active_idx:
            if is_skipped:
                state, sub = "skipped", default_sub
            else:
                state, sub = "completed", "Done"
        elif idx == active_idx:
            if status in ("failed", "analysis_failed"):
                state, sub = "failed", "Failed"
            elif status == "cancelled":
                state, sub = "cancelled", "Cancelled"
            elif is_skipped:
                state, sub = "skipped", default_sub
            elif is_waiting:
                state, sub = "waiting", "Paused"
            else:
                state, sub = "running", "Active"
        else:
            state = "skipped" if is_skipped else "pending"
            sub = default_sub if is_skipped else "Pending"
        results.append({"key": key, "title": title, "subtitle": sub, "state": state})
    return results


class _SourceThumbnail(QWidget):
    """Compact rounded thumbnail preview with clean YouTube/Local fallback icon."""

    def __init__(self, size: int = 56, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(size, size)
        self._image = QImage()
        self._is_youtube = True

    def set_source(self, thumb_path: str, is_youtube: bool) -> None:
        path = Path(thumb_path) if thumb_path else None
        self._image = QImage(str(path)) if path and path.is_file() else QImage()
        self._is_youtube = is_youtube
        self.update()

    def paintEvent(self, _event) -> None:
        t = theme_manager().tokens
        painter = QPainter(self)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        bounds = QRectF(self.rect())
        clip = QPainterPath()
        clip.addRoundedRect(bounds, 8, 8)
        painter.setClipPath(clip)
        painter.fillRect(bounds, QColor(t["bg_elevated"]))
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
            icon_name = "globe" if self._is_youtube else "film"
            icon_color = t["brand_primary"] if self._is_youtube else t["text_secondary"]
            pixmap = lucide_icon(icon_name, icon_color, max(18, self.width() // 2)).pixmap(
                max(18, self.width() // 2), max(18, self.width() // 2),
            )
            px = (bounds.width() - pixmap.deviceIndependentSize().width()) / 2.0
            py = (bounds.height() - pixmap.deviceIndependentSize().height()) / 2.0
            painter.drawPixmap(int(px), int(py), pixmap)
        painter.setClipping(False)
        painter.setPen(QColor(t["border_subtle"]))
        painter.drawRoundedRect(bounds.adjusted(0.5, 0.5, -0.5, -0.5), 8, 8)


class QueueOverviewBar(QFrame):
    """Top KPI strip with 4 dynamic status counters, engine/profile badges, and queue toggle."""

    pause_toggled = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("card")
        self._paused = False
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(8)

        cards_grid = QGridLayout()
        cards_grid.setContentsMargins(0, 0, 0, 0)
        cards_grid.setSpacing(8)

        self._counters: dict[str, QLabel] = {}
        self._tiles: list[tuple[QFrame, QLabel, QLabel]] = []
        specs = (
            ("processing", "Processing", "#E53935"),
            ("queued", "Queued", "#F5A623"),
            ("completed", "Completed", "#2ECC71"),
            ("failed", "Failed", "#FF6B7A"),
        )
        for col, (key, label_text, dot_color) in enumerate(specs):
            tile = QFrame(objectName="summaryPill")
            tile.setMinimumWidth(0)
            tile.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            tile_lay = QVBoxLayout(tile)
            tile_lay.setContentsMargins(10, 6, 10, 6)
            tile_lay.setSpacing(2)

            head = QHBoxLayout()
            head.setContentsMargins(0, 0, 0, 0)
            head.setSpacing(6)
            dot = QFrame()
            dot.setFixedSize(7, 7)
            dot.setStyleSheet(f"background-color: {dot_color}; border: none; border-radius: 3px;")
            lbl = QLabel(label_text, objectName="muted")
            lbl.setMinimumWidth(0)
            head.addWidget(dot)
            head.addWidget(lbl, 1)

            val = QLabel("0", objectName="summaryValue")
            val.setMinimumWidth(0)
            self._counters[key] = val
            self._tiles.append((tile, lbl, val))

            tile_lay.addLayout(head)
            tile_lay.addWidget(val)
            cards_grid.addWidget(tile, 0, col)
            cards_grid.setColumnStretch(col, 1)
        root.addLayout(cards_grid)

        controls = QHBoxLayout()
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(8)

        self.engine_badge = QLabel("Engine: Idle", objectName="statusChip")
        self.profile_badge = QLabel("Profile: Auto", objectName="statusChip")
        controls.addWidget(self.engine_badge)
        controls.addWidget(self.profile_badge)
        controls.addStretch()

        self.pause_queue_button = QPushButton("Pause Queue")
        self.pause_queue_button.setIcon(lucide_icon("pause", theme_manager().tokens["text_primary"], 14))
        self.pause_queue_button.setIconSize(QSize(14, 14))
        self.pause_queue_button.clicked.connect(self._toggle_pause)
        controls.addWidget(self.pause_queue_button)
        root.addLayout(controls)

    def apply_theme(self, _resolved: str = "") -> None:
        icon_fg = theme_manager().tokens["text_primary"]
        self.pause_queue_button.setIcon(lucide_icon("play" if self._paused else "pause", icon_fg, 14))

    def _toggle_pause(self) -> None:
        self._paused = not self._paused
        self._sync_pause_button()
        self.pause_toggled.emit(self._paused)

    def set_paused(self, paused: bool) -> None:
        self._paused = bool(paused)
        self._sync_pause_button()

    def _sync_pause_button(self) -> None:
        fg = theme_manager().tokens["text_primary"]
        if self._paused:
            self.pause_queue_button.setText("Resume Queue")
            self.pause_queue_button.setIcon(lucide_icon("play", fg, 14))
        else:
            self.pause_queue_button.setText("Pause Queue")
            self.pause_queue_button.setIcon(lucide_icon("pause", fg, 14))

    def update_summary(
        self,
        projects: list[dict[str, Any]],
        *,
        engine_state: str,
        profile_name: str,
        paused: bool,
    ) -> None:
        counts = {"processing": 0, "queued": 0, "completed": 0, "failed": 0}
        for row in projects:
            st = str(row.get("status") or "")
            if st == "processing":
                counts["processing"] += 1
            elif st == "queued":
                counts["queued"] += 1
            elif st == "completed":
                counts["completed"] += 1
            elif st in ("failed", "analysis_failed"):
                counts["failed"] += 1
        for key, label in self._counters.items():
            label.setText(str(counts[key]))
        self._paused = paused
        self._sync_pause_button()

        state_label = "Paused" if paused and engine_state == "Idle" else engine_state
        if state_label in ("Processing", "Active"):
            tone = "danger"
        elif state_label in ("Waiting", "Paused"):
            tone = "warning"
        else:
            tone = ""
        self.engine_badge.setText(f"Engine: {state_label}")
        self.engine_badge.setStyleSheet("")
        self.engine_badge.setProperty("tone", tone)
        self.engine_badge.style().unpolish(self.engine_badge)
        self.engine_badge.style().polish(self.engine_badge)
        nice_profile = (profile_name or "Auto").replace("_", " ").title()
        self.profile_badge.setText(f"Profile: {nice_profile}")


class PipelineStepperWidget(QFrame):
    """Horizontal 6-stage theme-aware media processing pipeline stepper."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._last_stages: list[dict[str, str]] = []
        grid = QGridLayout(self)
        grid.setContentsMargins(8, 6, 8, 6)
        grid.setSpacing(6)
        self._chips: list[tuple[QFrame, QFrame, QLabel, QLabel]] = []
        for idx in range(6):
            chip = QFrame()
            chip.setMinimumWidth(0)
            chip.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            chip_lay = QVBoxLayout(chip)
            chip_lay.setContentsMargins(6, 5, 6, 5)
            chip_lay.setSpacing(2)

            top = QHBoxLayout()
            top.setContentsMargins(0, 0, 0, 0)
            top.setSpacing(4)
            dot = QFrame()
            dot.setFixedSize(7, 7)
            title = QLabel("Stage")
            title.setMinimumWidth(0)
            title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            title.setWordWrap(True)
            top.addWidget(dot, 0, Qt.AlignmentFlag.AlignTop)
            top.addWidget(title, 1)

            sub = QLabel("Pending")
            sub.setMinimumWidth(0)
            sub.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

            chip_lay.addLayout(top)
            chip_lay.addWidget(sub)
            grid.addWidget(chip, 0, idx)
            grid.setColumnStretch(idx, 1)
            self._chips.append((chip, dot, title, sub))
        self.apply_theme()

    def apply_theme(self, _resolved: str = "") -> None:
        t = theme_manager().tokens
        self.setStyleSheet(
            f"PipelineStepperWidget {{ background-color: {t['bg_alt_row']}; "
            f"border: 1px solid {t['border_subtle']}; border-radius: 10px; }}"
        )
        if self._last_stages:
            self.set_stages(self._last_stages)

    def set_stages(self, stages: list[dict[str, str]]) -> None:
        self._last_stages = list(stages)
        t = theme_manager().tokens
        for idx, (chip, dot, title, sub) in enumerate(self._chips):
            if idx >= len(stages):
                continue
            item = stages[idx]
            state = item.get("state", "pending")
            accent, fg, bg, border_color = _stage_palette(state)
            chip.setStyleSheet(
                f"QFrame {{ background-color: {bg}; border: 1px solid {border_color}; border-radius: 8px; }}"
            )
            dot.setStyleSheet(f"background-color: {accent}; border: none; border-radius: 3px;")
            title.setText(item.get("title", ""))
            title_color = t["text_disabled"] if state == "skipped" else t["text_primary"]
            title.setStyleSheet(
                f"font-size: 11px; font-weight: 600; color: {title_color}; "
                "border: none; background: transparent;"
            )
            sub.setText(item.get("subtitle", ""))
            sub.setStyleSheet(f"font-size: 10px; font-weight: 600; color: {fg}; border: none; background: transparent;")


class ActiveJobCard(QFrame):
    """Focused active processing card preserving download_view and speech_progress hooks."""

    new_project_requested = Signal()

    def __init__(self, on_cancel, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("card")
        self._started_at: float | None = None
        self._stage_started_at: float | None = None
        self._active_stage_eta: float | None = None
        self._stage_has_reliable_progress: bool = False
        self._progress_value: int = 0
        self._status: str = "idle"
        self._options: dict[str, Any] = {}
        self._stage_message: str = ""
        self._tick = QTimer(self)
        self._tick.setInterval(1000)
        self._tick.timeout.connect(self._refresh_timing)

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(0)

        self.stack = QStackedWidget()
        root.addWidget(self.stack)

        # Page 0: Compact Idle State
        idle_widget = QWidget()
        idle_lay = QHBoxLayout(idle_widget)
        idle_lay.setContentsMargins(2, 4, 2, 4)
        idle_lay.setSpacing(12)
        self._idle_icon = QLabel(objectName="summaryPill")
        self._idle_icon.setFixedSize(40, 40)
        self._idle_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._idle_icon.setPixmap(lucide_icon("clock", theme_manager().tokens["text_secondary"], 20).pixmap(20, 20))
        idle_text = QVBoxLayout()
        idle_text.setSpacing(2)
        idle_title = QLabel("No active processing job", objectName="cardTitle")
        idle_sub = QLabel("Queue a video from New Project or resume paused items below.")
        idle_sub.setObjectName("muted")
        idle_sub.setWordWrap(True)
        idle_text.addWidget(idle_title)
        idle_text.addWidget(idle_sub)
        idle_btn = QPushButton("New Project")
        idle_btn.setObjectName("primary")
        idle_btn.clicked.connect(self.new_project_requested.emit)
        idle_lay.addWidget(self._idle_icon)
        idle_lay.addLayout(idle_text, 1)
        idle_lay.addWidget(idle_btn)
        self.stack.addWidget(idle_widget)

        # Page 1: Active Job Card
        active_widget = QWidget()
        active_lay = QVBoxLayout(active_widget)
        active_lay.setContentsMargins(0, 0, 0, 0)
        active_lay.setSpacing(10)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(12)
        self.thumbnail = _SourceThumbnail(52)
        header.addWidget(self.thumbnail)

        meta_col = QVBoxLayout()
        meta_col.setSpacing(4)
        title_line = QHBoxLayout()
        title_line.setSpacing(8)
        self.title_label = QLabel("Active Project", objectName="cardTitle")
        self.title_label.setMinimumWidth(0)
        self.source_pill = QLabel("YouTube", objectName="statusChip")
        title_line.addWidget(self.title_label, 1)
        title_line.addWidget(self.source_pill)

        self.media_label = QLabel("Waiting for stage updates")
        self.media_label.setObjectName("muted")
        self.media_label.setMinimumWidth(0)
        meta_col.addLayout(title_line)
        meta_col.addWidget(self.media_label)
        header.addLayout(meta_col, 1)

        self.cancel_button = QPushButton("Cancel Job", objectName="danger")
        self.cancel_button.setIcon(lucide_icon("x", theme_manager().tokens["semantic_danger_fg"], 14))
        self.cancel_button.setIconSize(QSize(14, 14))
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(on_cancel)
        header.addWidget(self.cancel_button, 0, Qt.AlignmentFlag.AlignTop)
        active_lay.addLayout(header)

        self.stepper = PipelineStepperWidget()
        active_lay.addWidget(self.stepper)

        self.processing_status_row = QWidget()
        status_row_lay = QHBoxLayout(self.processing_status_row)
        status_row_lay.setContentsMargins(0, 0, 0, 0)
        status_row_lay.setSpacing(8)
        self.queue_status = QLabel("Idle", objectName="transferStatus")
        self.queue_status.setWordWrap(True)
        self.queue_status.setTextFormat(Qt.TextFormat.PlainText)
        self.queue_status.setMinimumWidth(0)
        status_row_lay.addWidget(self.queue_status, 1)
        active_lay.addWidget(self.processing_status_row)

        self.processing_progress_row = QWidget()
        progress_row_lay = QHBoxLayout(self.processing_progress_row)
        progress_row_lay.setContentsMargins(0, 0, 0, 0)
        progress_row_lay.setSpacing(10)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setAccessibleName("Project progress")
        self.progress_text = QLabel("0%", objectName="transferMetric")
        progress_row_lay.addWidget(self.progress_bar, 1)
        progress_row_lay.addWidget(self.progress_text)
        active_lay.addWidget(self.processing_progress_row)

        self.download_view = DownloadView()
        self.download_view.cancel_button.clicked.connect(on_cancel)
        self.download_view.hide()
        active_lay.addWidget(self.download_view)

        self.speech_progress = SpeechProgressView()
        self.speech_progress.cancel_button.clicked.connect(on_cancel)
        self.speech_progress.hide()
        active_lay.addWidget(self.speech_progress)

        footer = QHBoxLayout()
        footer.setContentsMargins(0, 2, 0, 0)
        footer.setSpacing(14)
        self.elapsed_label = QLabel("Job elapsed: 00:00")
        self.elapsed_label.setObjectName("muted")
        self.eta_label = QLabel("Overall ETA: Estimating…")
        self.eta_label.setObjectName("muted")
        self.profile_label = QLabel("Profile: Auto")
        self.profile_label.setObjectName("muted")
        footer.addWidget(self.elapsed_label)
        footer.addWidget(self.eta_label)
        footer.addStretch()
        footer.addWidget(self.profile_label)
        active_lay.addLayout(footer)

        self.stack.addWidget(active_widget)
        self._set_active_page(False)

    def apply_theme(self, _resolved: str = "") -> None:
        t = theme_manager().tokens
        self._idle_icon.setPixmap(lucide_icon("clock", t["text_secondary"], 20).pixmap(20, 20))
        self.cancel_button.setIcon(lucide_icon("x", t["semantic_danger_fg"], 14))
        self.stepper.apply_theme(_resolved)
        self.thumbnail.update()

    def set_stage_telemetry(self, *, eta_seconds: float | None, reliable: bool) -> None:
        """Feed real stage-level ETA (e.g. from MLX/Faster-Whisper or yt-dlp) into overall job ETA."""
        self._stage_has_reliable_progress = bool(reliable)
        self._active_stage_eta = eta_seconds if (eta_seconds is not None and eta_seconds >= 0) else None
        self._refresh_timing()

    def elapsed_seconds(self) -> float | None:
        if self._started_at is None:
            return None
        return max(0.0, time.monotonic() - self._started_at)

    def _set_active_page(self, active: bool) -> None:
        idx = 1 if active else 0
        self.stack.setCurrentIndex(idx)
        for i in (0, 1):
            w = self.stack.widget(i)
            if w is None:
                continue
            if i == idx:
                w.setSizePolicy(
                    QSizePolicy.Policy.Expanding,
                    QSizePolicy.Policy.Minimum if active else QSizePolicy.Policy.Maximum,
                )
            else:
                w.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.stack.setMaximumHeight(16777215 if active else 84)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Minimum if active else QSizePolicy.Policy.Maximum,
        )
        self.stack.updateGeometry()
        self.updateGeometry()

    def start_job(
        self,
        project: dict[str, Any] | None,
        *,
        options_dict: dict[str, Any],
        profile_name: str,
        initial_progress: int = 0,
        status_message: str = "Starting pipeline",
        thumb_path: str = "",
    ) -> None:
        self._started_at = time.monotonic()
        self._stage_started_at = self._started_at
        self._active_stage_eta = None
        self._stage_has_reliable_progress = False
        self._progress_value = max(0, min(100, int(initial_progress)))
        self._status = "processing"
        self._options = dict(options_dict)
        self._stage_message = status_message

        title = project_display_name(project) if project else (options_dict.get("project_name") or "Active Project")
        source = str(options_dict.get("source") or (project or {}).get("source") or "")
        is_yt = is_youtube_url(source) if source else True
        self.thumbnail.set_source(thumb_path, is_yt)
        self.title_label.setText(title)
        self.source_pill.setText("YouTube" if is_yt else "Local File")
        media = project_media_summary(project or {"source": source, "options": options_dict})
        self.media_label.setText(media)
        self.queue_status.setText(status_message)
        self.progress_bar.setValue(self._progress_value)
        self.progress_text.setText(f"{self._progress_value}%")
        nice_profile = (profile_name or "Auto").replace("_", " ").title()
        self.profile_label.setText(f"Profile: {nice_profile}")
        self.cancel_button.setEnabled(True)
        self.stepper.set_stages(compute_pipeline_stages(self._options, "processing", self._progress_value, status_message))
        self._refresh_timing()
        self._tick.start()
        self._set_active_page(True)

    def ensure_active_visible(self) -> None:
        if self.stack.currentIndex() != 1:
            self._set_active_page(True)

    def update_stage(self, progress: int, message: str) -> None:
        self.ensure_active_visible()
        if progress:
            self._progress_value = max(0, min(100, int(progress)))
            self.progress_bar.setValue(self._progress_value)
            self.progress_text.setText(f"{self._progress_value}%")
        self._stage_message = redact(message).partition("\n")[0]
        self.queue_status.setText(self._stage_message)
        self.stepper.set_stages(
            compute_pipeline_stages(self._options, "processing", self._progress_value, self._stage_message)
        )
        self._refresh_timing()

    def mark_finished(self, status: str, summary: str) -> None:
        self._tick.stop()
        self._status = status
        if status == "completed":
            self._progress_value = 100
            self.progress_bar.setValue(100)
            self.progress_text.setText("100%")
        self.cancel_button.setEnabled(False)
        self.queue_status.setText(summary)
        self.stepper.set_stages(
            compute_pipeline_stages(self._options, status, self._progress_value, summary)
        )
        self.eta_label.setText("ETA: —")

    def show_idle(self) -> None:
        self._tick.stop()
        self._status = "idle"
        self._started_at = None
        self.cancel_button.setEnabled(False)
        self._set_active_page(False)

    def _refresh_timing(self) -> None:
        if self._started_at is None:
            self.elapsed_label.setText("Job elapsed: 00:00")
            self.eta_label.setText("Overall ETA: —")
            return
        elapsed = time.monotonic() - self._started_at
        self.elapsed_label.setText(f"Job elapsed: {format_elapsed(elapsed)}")
        # Do not fabricate misleading overall ETAs while transcription/download has no measurable progress!
        in_speech = not self.speech_progress.isHidden()
        in_download = not self.download_view.isHidden()
        if (in_speech or in_download) and not self._stage_has_reliable_progress:
            self.eta_label.setText("Overall ETA: Estimating…")
            return
        if in_speech and self._active_stage_eta is not None:
            # Estimate remaining post-transcription work conservatively based on clip count
            clip_count = max(1, int(self._options.get("clip_count") or 3))
            post_est = 12.0 + clip_count * 12.0
            self.eta_label.setText(f"Overall ETA: ~{format_elapsed(self._active_stage_eta + post_est)}")
            return
        eta = estimate_eta_text(self._started_at, self._progress_value, status=self._status)
        self.eta_label.setText(f"Overall ETA: {eta if eta != '—' else 'Estimating…'}")


class JobDetailsDialog(QDialog):
    """Modal inspector showing project configuration, pipeline stage state, and full error details."""

    def __init__(self, project: dict[str, Any], clips_count: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Job Details — {project_display_name(project)}")
        self.resize(560, 440)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        options = parse_project_options(project)
        src_type, src_detail = project_source_info(project)
        status = str(project.get("status") or "queued")
        label, _accent, _bg, _fg = QUEUE_STATUS_META.get(status, QUEUE_STATUS_META["queued"])

        t = theme_manager().tokens
        head = QLabel(project_display_name(project), objectName="sectionHeading")
        head.setWordWrap(True)
        layout.addWidget(head)

        meta = QLabel(
            f"Status: {label} ({int(project.get('progress') or 0)}%)  •  Source: {src_type}\n"
            f"Media: {project_media_summary(project)}  •  Generated Clips: {clips_count}\n"
            f"Created: {format_timestamp_short(project.get('created_at'))}\n"
            f"Provider: {options.get('provider', '—')} ({options.get('model') or 'default'})"
        )
        meta.setWordWrap(True)
        meta.setObjectName("muted")
        layout.addWidget(meta)

        if src_detail:
            src_box = QLabel(f"Source reference: {src_detail}")
            src_box.setWordWrap(True)
            src_box.setStyleSheet(
                f"background-color: {t['bg_elevated']}; border: 1px solid {t['border_subtle']}; border-radius: 8px; "
                f"padding: 8px 10px; font-size: 12px; color: {t['text_primary']};"
            )
            layout.addWidget(src_box)

        stepper = PipelineStepperWidget()
        stepper.set_stages(
            compute_pipeline_stages(options, status, int(project.get("progress") or 0), str(project.get("stage") or ""))
        )
        layout.addWidget(stepper)

        err = redact(str(project.get("error") or "")).strip()
        if err:
            err_title = QLabel("Failure Details")
            err_title.setStyleSheet(f"font-size: 12px; font-weight: 700; color: {t['semantic_danger_fg']};")
            layout.addWidget(err_title)
            err_box = QPlainTextEdit()
            err_box.setReadOnly(True)
            err_box.setPlainText(err)
            err_box.setMaximumHeight(160)
            layout.addWidget(err_box)

        layout.addStretch()
        btn_row = QHBoxLayout()
        if err:
            copy_btn = QPushButton("Copy Diagnostics")
            copy_btn.clicked.connect(lambda: QApplication.clipboard().setText(err))
            btn_row.addWidget(copy_btn)
        btn_row.addStretch()
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)


class ErrorDetailsDialog(QDialog):
    """Actionable failure dialog with concise headline, expandable technical diagnostics, Copy, Retry, and Dependency Manager."""

    def __init__(
        self,
        title: str,
        message: str,
        *,
        allow_retry: bool = False,
        allow_dependencies: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(520)
        self.chosen_action = "close"

        redacted = redact(message).strip()
        summary, sep, details = redacted.partition("\n\nTechnical details:\n")
        if not sep and "\n" in redacted:
            summary, _, details = redacted.partition("\n")
        self._full_diagnostics = redacted

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        headline = QLabel(summary.strip() or "Processing failed.")
        headline.setWordWrap(True)
        headline.setStyleSheet("font-size: 14px; font-weight: 600;")
        layout.addWidget(headline)

        self.details_view = QPlainTextEdit()
        self.details_view.setReadOnly(True)
        self.details_view.setPlainText(details.strip() or redacted)
        self.details_view.setMinimumHeight(140)
        self.details_view.setVisible(False)

        self.toggle_btn = QPushButton("Show Technical Details")
        self.toggle_btn.clicked.connect(self._toggle_details)
        layout.addWidget(self.toggle_btn, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.details_view)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        copy_btn = QPushButton("Copy Diagnostics")
        copy_btn.clicked.connect(self._copy)
        actions.addWidget(copy_btn)

        if allow_dependencies:
            dep_btn = QPushButton("Open Dependency Manager")
            dep_btn.clicked.connect(lambda: self._finish("dependencies"))
            actions.addWidget(dep_btn)

        actions.addStretch()

        if allow_retry:
            retry_btn = QPushButton("Retry", objectName="primary")
            retry_btn.clicked.connect(lambda: self._finish("retry"))
            actions.addWidget(retry_btn)

        close_btn = QPushButton("Close", objectName="" if allow_retry else "primary")
        close_btn.clicked.connect(self.accept)
        actions.addWidget(close_btn)
        layout.addLayout(actions)

    def _toggle_details(self) -> None:
        showing = not self.details_view.isVisible()
        self.details_view.setVisible(showing)
        self.toggle_btn.setText("Hide Technical Details" if showing else "Show Technical Details")
        self.adjustSize()

    def _copy(self) -> None:
        QApplication.clipboard().setText(self._full_diagnostics)

    def _finish(self, action: str) -> None:
        self.chosen_action = action
        self.accept()


class JobCompletedDialog(QDialog):
    """Non-disruptive completion popup celebrating finished clips with direct navigation to Generated Clips."""

    view_clips_requested = Signal(str)  # project_id

    def __init__(
        self,
        project_id: str,
        project_title: str,
        clip_count: int,
        combined_duration: float,
        processing_seconds: float | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.project_id = project_id
        self.setWindowTitle("Clips Ready — AutoClip Studio")
        self.setMinimumWidth(440)
        self.setModal(False)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        t = theme_manager().tokens

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(14)

        top = QHBoxLayout()
        top.setSpacing(12)
        icon_badge = QLabel()
        icon_badge.setFixedSize(44, 44)
        icon_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_badge.setStyleSheet(
            f"background-color: {t['semantic_success_bg']}; border: 1px solid {t['semantic_success']}; border-radius: 22px;"
        )
        icon_badge.setPixmap(lucide_icon("check", t["semantic_success"], 22).pixmap(22, 22))
        top.addWidget(icon_badge, 0, Qt.AlignmentFlag.AlignTop)

        head_col = QVBoxLayout()
        head_col.setSpacing(4)
        heading = QLabel("Your clips are ready!", objectName="sectionHeading")
        title_lbl = QLabel(project_title or "Completed Project", objectName="cardTitle")
        title_lbl.setWordWrap(True)
        head_col.addWidget(heading)
        head_col.addWidget(title_lbl)
        top.addLayout(head_col, 1)
        layout.addLayout(top)

        stats_card = QFrame(objectName="summaryPill")
        stats_lay = QGridLayout(stats_card)
        stats_lay.setContentsMargins(14, 10, 14, 10)
        stats_lay.setHorizontalSpacing(16)
        stats_lay.setVerticalSpacing(4)
        metrics: list[tuple[str, str]] = [
            ("Generated clips", f"{clip_count} clip{'s' if clip_count != 1 else ''}"),
            ("Combined duration", _format_mmss(combined_duration) or "0:00"),
        ]
        if processing_seconds is not None and processing_seconds >= 0.5:
            metrics.append(("Processing time", format_elapsed(processing_seconds)))
        for col, (lbl_text, val_text) in enumerate(metrics):
            lbl = QLabel(lbl_text, objectName="muted")
            val = QLabel(val_text, objectName="summaryValue")
            stats_lay.addWidget(lbl, 0, col)
            stats_lay.addWidget(val, 1, col)
            stats_lay.setColumnStretch(col, 1)
        layout.addWidget(stats_card)

        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        self.stay_btn = QPushButton("Stay on Processing Queue")
        self.stay_btn.clicked.connect(self.reject)
        self.view_btn = QPushButton("View Generated Clips", objectName="primary")
        self.view_btn.setDefault(True)
        self.view_btn.setIcon(lucide_icon("film", "#FFFFFF", 15))
        self.view_btn.clicked.connect(self._open_clips)
        buttons.addStretch()
        buttons.addWidget(self.stay_btn)
        buttons.addWidget(self.view_btn)
        layout.addLayout(buttons)

    def _open_clips(self) -> None:
        self.view_clips_requested.emit(self.project_id)
        self.accept()


class JobRowCard(QFrame):
    """Modern SaaS queue/history card with full Light/Dark contrast, contextual CTA, and overflow menu."""

    clicked = Signal(str)
    action_triggered = Signal(str, str)  # action_name, project_id

    def __init__(
        self,
        project: dict[str, Any],
        *,
        thumb_path: str = "",
        clip_count: int = 0,
        can_move_up: bool = False,
        can_move_down: bool = False,
        selected: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.project = project
        self.project_id = str(project["id"])
        self.setObjectName("queueRowCard")
        self.setProperty("selected", "true" if selected else "false")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self.setMinimumHeight(66)
        self.setMinimumWidth(0)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        t = theme_manager().tokens
        status = str(project.get("status") or "queued")
        progress = int(project.get("progress") or 0)
        src_type, src_detail = project_source_info(project)
        is_yt = src_type == "YouTube"
        status_label, status_tone = _status_chip_style(status)

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 11, 12, 11)
        root.setSpacing(8)

        main = QHBoxLayout()
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(12)

        thumb = _SourceThumbnail(44)
        thumb.set_source(thumb_path, is_yt)
        main.addWidget(thumb, 0, Qt.AlignmentFlag.AlignTop)

        info = QVBoxLayout()
        info.setSpacing(3)
        title_lbl = QLabel(project_display_name(project))
        title_lbl.setMinimumWidth(0)
        title_lbl.setWordWrap(True)
        title_lbl.setStyleSheet(f"font-size: 13px; font-weight: 600; color: {t['text_primary']}; background: transparent;")
        title_lbl.setToolTip(src_detail or project_display_name(project))

        sub_parts = [src_type, project_media_summary(project), format_timestamp_short(project.get("created_at"))]
        if status == "processing":
            sub_parts.append(f"{progress}%")
        if clip_count:
            sub_parts.append(f"{clip_count} clip{'s' if clip_count != 1 else ''}")
        sub_lbl = QLabel(" • ".join(p for p in sub_parts if p), objectName="muted")
        sub_lbl.setMinimumWidth(0)
        sub_lbl.setWordWrap(True)
        info.addWidget(title_lbl)
        info.addWidget(sub_lbl)
        main.addLayout(info, 1)

        right = QHBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(8)

        status_pill = QLabel(status_label, objectName="statusChip")
        status_pill.setProperty("tone", status_tone)
        right.addWidget(status_pill, 0, Qt.AlignmentFlag.AlignVCenter)

        # Contextual primary action per job state
        self.primary_action_btn: QPushButton | None = None
        if status == "analysis_failed":
            btn = QPushButton("Retry AI Analysis", objectName="primary")
            btn.clicked.connect(lambda: self.action_triggered.emit("retry_analysis", self.project_id))
            self.primary_action_btn = btn
        elif status == "failed":
            btn = QPushButton("Retry Failed Stage")
            btn.clicked.connect(lambda: self.action_triggered.emit("retry_failed", self.project_id))
            self.primary_action_btn = btn
        elif status == "processing":
            btn = QPushButton("Cancel Job", objectName="danger")
            btn.clicked.connect(lambda: self.action_triggered.emit("cancel_active", self.project_id))
            self.primary_action_btn = btn
        elif status == "completed":
            btn = QPushButton("View Generated Clips")
            btn.clicked.connect(lambda: self.action_triggered.emit("view_clips", self.project_id))
            self.primary_action_btn = btn
        elif status == "queued":
            btn = QPushButton("Cancel")
            btn.clicked.connect(lambda: self.action_triggered.emit("cancel_queued", self.project_id))
            self.primary_action_btn = btn

        if self.primary_action_btn is not None:
            right.addWidget(self.primary_action_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        menu_btn = QPushButton(objectName="iconButton")
        menu_btn.setFixedSize(30, 30)
        menu_btn.setToolTip("More job actions")
        menu_btn.setAccessibleName("More job actions")
        menu_btn.setIcon(lucide_icon("more-horizontal", t["text_secondary"], 15))
        menu_btn.setIconSize(QSize(15, 15))
        menu_btn.clicked.connect(
            lambda: self._open_context_menu(menu_btn, status, clip_count, can_move_up, can_move_down)
        )
        right.addWidget(menu_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        main.addLayout(right)
        root.addLayout(main)

        err_full = redact(str(project.get("error") or "")).strip()
        if err_full:
            err_summary = err_full.splitlines()[0]
            if len(err_summary) > 110:
                err_summary = err_summary[:107] + "…"
            err_bar = QHBoxLayout()
            err_bar.setContentsMargins(56, 0, 0, 0)
            err_bar.setSpacing(8)
            err_lbl = QLabel(err_summary)
            err_lbl.setMinimumWidth(0)
            err_lbl.setWordWrap(True)
            err_lbl.setStyleSheet(f"font-size: 12px; color: {t['semantic_danger_fg']}; background: transparent;")
            err_bar.addWidget(err_lbl, 1)

            self.details_box = QLabel(err_full)
            self.details_box.setWordWrap(True)
            self.details_box.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self.details_box.setVisible(False)
            self.details_box.setStyleSheet(
                f"background-color: {t['bg_elevated']}; border: 1px solid {t['semantic_danger_border']}; "
                f"border-radius: 8px; padding: 8px 10px; margin-left: 56px; font-size: 11px; color: {t['text_primary']};"
            )

            copy_err_btn = QPushButton("Copy", objectName="ghost")
            copy_err_btn.setFlat(True)
            copy_err_btn.setStyleSheet(f"QPushButton {{ color: {t['text_secondary']}; font-size: 11px; padding: 2px 6px; }}")
            copy_err_btn.clicked.connect(lambda _c=False, txt=err_full: QApplication.clipboard().setText(txt))

            self.toggle_err_btn = QPushButton("Show details")
            self.toggle_err_btn.setFlat(True)
            self.toggle_err_btn.setStyleSheet(
                f"QPushButton {{ color: {t['brand_accent']}; font-size: 11px; font-weight: 600; border: none; padding: 2px 6px; }}"
            )
            self.toggle_err_btn.clicked.connect(self._toggle_error_details)
            err_bar.addWidget(copy_err_btn)
            err_bar.addWidget(self.toggle_err_btn)
            root.addLayout(err_bar)
            root.addWidget(self.details_box)

    def _toggle_error_details(self) -> None:
        showing = not self.details_box.isVisible()
        self.details_box.setVisible(showing)
        self.toggle_err_btn.setText("Hide details" if showing else "Show details")

    def _open_context_menu(
        self,
        anchor: QPushButton,
        status: str,
        clip_count: int,
        can_move_up: bool,
        can_move_down: bool,
    ) -> None:
        t = theme_manager().tokens
        fg = t["text_primary"]
        danger = t["semantic_danger_fg"]
        menu = QMenu(self)
        act_details = menu.addAction(lucide_icon("external-link", fg, 14), "View Details")
        act_details.triggered.connect(lambda: self.action_triggered.emit("view_details", self.project_id))

        act_folder = menu.addAction(lucide_icon("folder-open", fg, 14), "Open Project Folder")
        act_folder.triggered.connect(lambda: self.action_triggered.emit("open_folder", self.project_id))

        if clip_count > 0 or status == "completed":
            act_clips = menu.addAction(lucide_icon("film", fg, 14), "View Generated Clips")
            act_clips.triggered.connect(lambda: self.action_triggered.emit("view_clips", self.project_id))

        menu.addSeparator()
        if status == "processing":
            act_cancel_active = menu.addAction(lucide_icon("x", danger, 14), "Cancel Job")
            act_cancel_active.triggered.connect(lambda: self.action_triggered.emit("cancel_active", self.project_id))
        elif status == "queued":
            if can_move_up:
                act_up = menu.addAction(lucide_icon("arrow-up", fg, 14), "Move Up in Queue")
                act_up.triggered.connect(lambda: self.action_triggered.emit("move_up", self.project_id))
            if can_move_down:
                act_down = menu.addAction(lucide_icon("arrow-down", fg, 14), "Move Down in Queue")
                act_down.triggered.connect(lambda: self.action_triggered.emit("move_down", self.project_id))
            act_cancel_q = menu.addAction(lucide_icon("x", danger, 14), "Cancel Queued Job")
            act_cancel_q.triggered.connect(lambda: self.action_triggered.emit("cancel_queued", self.project_id))
        elif status == "analysis_failed":
            act_retry_ai = menu.addAction(lucide_icon("refresh-cw", t["brand_accent"], 14), "Retry AI Analysis")
            act_retry_ai.triggered.connect(lambda: self.action_triggered.emit("retry_analysis", self.project_id))
        elif status in ("failed", "cancelled"):
            act_retry_stage = menu.addAction(lucide_icon("refresh-cw", fg, 14), "Retry Failed Stage")
            act_retry_stage.triggered.connect(lambda: self.action_triggered.emit("retry_failed", self.project_id))

        if status != "processing":
            menu.addSeparator()
            act_remove = menu.addAction(lucide_icon("trash-2", danger, 14), "Remove from History")
            act_remove.triggered.connect(lambda: self.action_triggered.emit("remove_history", self.project_id))

        menu.exec(anchor.mapToGlobal(anchor.rect().bottomLeft()))

    def set_selected(self, selected: bool) -> None:
        self.setProperty("selected", "true" if selected else "false")
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.project_id)
        super().mousePressEvent(event)


class JobQueuePanel(QFrame):
    """Filterable, searchable, sortable job queue & history list."""

    selection_changed = Signal()
    action_triggered = Signal(str, str)  # action_name, project_id

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("card")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumHeight(170)
        self.setMinimumWidth(0)
        self._projects: list[dict[str, Any]] = []
        self._clips: list[dict[str, Any]] = []
        self._visible_projects: list[dict[str, Any]] = []
        self._selected_id: str | None = None
        self._cards: dict[str, JobRowCard] = {}
        self._filter_key = "all"

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(8)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)
        title = QLabel("Job Queue & History", objectName="sectionTitle")
        header.addWidget(title, 1)
        # Contextual retry buttons live on each failed card; keep hidden alias for backward compatibility.
        self.retry_analysis_button = QPushButton("Retry AI Analysis")
        self.retry_analysis_button.setEnabled(False)
        self.retry_analysis_button.hide()
        root.addLayout(header)

        # Filter tabs row
        tabs_row = QHBoxLayout()
        tabs_row.setContentsMargins(0, 0, 0, 0)
        tabs_row.setSpacing(4)
        self._tab_group = QButtonGroup(self)
        self._tab_group.setExclusive(True)
        for key, label in (
            ("all", "All"),
            ("queued", "Queued"),
            ("processing", "Processing"),
            ("completed", "Completed"),
            ("failed", "Failed"),
            ("cancelled", "Cancelled"),
        ):
            btn = QPushButton(label, objectName="filterTab")
            btn.setCheckable(True)
            btn.setChecked(key == "all")
            btn.setMinimumWidth(0)
            btn.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
            btn.clicked.connect(lambda _checked, k=key: self._set_filter(k))
            self._tab_group.addButton(btn)
            tabs_row.addWidget(btn)
        tabs_row.addStretch()
        root.addLayout(tabs_row)

        # Search + Sort toolbar
        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(0, 0, 0, 0)
        toolbar.setSpacing(8)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search jobs by title, source, or status...")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.setMinimumWidth(0)
        self.search_input.textChanged.connect(self._rebuild_rows)

        self.sort_combo = QComboBox()
        self.sort_combo.addItems(["Recent Activity", "Queue Order", "Newest First", "Oldest First", "Progress", "Name"])
        self.sort_combo.setMinimumWidth(130)
        self.sort_combo.currentIndexChanged.connect(self._rebuild_rows)
        toolbar.addWidget(self.search_input, 1)
        toolbar.addWidget(self.sort_combo)
        root.addLayout(toolbar)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setMinimumHeight(96)
        self.list_container = QWidget()
        self.list_container.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Minimum)
        self.list_layout = QVBoxLayout(self.list_container)
        self.list_layout.setContentsMargins(0, 0, 2, 0)
        self.list_layout.setSpacing(8)
        self.list_layout.addStretch()
        self.scroll.setWidget(self.list_container)
        root.addWidget(self.scroll, 1)

    def _set_filter(self, key: str) -> None:
        self._filter_key = key
        self._rebuild_rows()

    def set_data(self, projects: list[dict[str, Any]], clips: list[dict[str, Any]]) -> None:
        self._projects = list(projects)
        self._clips = list(clips)
        self._rebuild_rows()

    def _filtered_sorted_projects(self) -> list[dict[str, Any]]:
        query = self.search_input.text().strip().lower()
        items: list[dict[str, Any]] = []
        for row in self._projects:
            status = str(row.get("status") or "")
            if self._filter_key == "failed":
                if status not in ("failed", "analysis_failed"):
                    continue
            elif self._filter_key != "all" and status != self._filter_key:
                continue
            if query:
                haystack = " ".join((
                    project_display_name(row),
                    str(row.get("source") or ""),
                    status,
                    str(row.get("error") or ""),
                )).lower()
                if query not in haystack:
                    continue
            items.append(row)

        mode = self.sort_combo.currentText()
        if mode == "Recent Activity":
            # Active processing first -> queued (in execution order) -> completed/failed/cancelled by newest timestamp!
            def _recent_key(r: dict[str, Any]) -> tuple:
                st = str(r.get("status") or "")
                ts = str(r.get("updated_at") or r.get("created_at") or "")
                if st == "processing":
                    return (0, 0, "")
                if st == "queued":
                    return (1, int(r.get("queue_order") or 0), ts)
                # Newly completed jobs appear right at the top of history, never buried below old failed jobs!
                return (2, 0, "")

            active = [r for r in items if str(r.get("status") or "") == "processing"]
            queued = sorted(
                (r for r in items if str(r.get("status") or "") == "queued"),
                key=lambda r: (int(r.get("queue_order") or 0), str(r.get("created_at") or "")),
            )
            history = sorted(
                (r for r in items if str(r.get("status") or "") not in ("processing", "queued")),
                key=lambda r: (
                    str(r.get("updated_at") or r.get("created_at") or ""),
                    1 if str(r.get("status") or "") == "completed" else 0,
                ),
                reverse=True,
            )
            items = active + queued + history
        elif mode == "Queue Order":
            rank = {"processing": 0, "queued": 1, "completed": 2, "analysis_failed": 3, "failed": 4, "cancelled": 5}
            items.sort(
                key=lambda r: (
                    rank.get(str(r.get("status") or ""), 9),
                    int(r.get("queue_order") or 0) if str(r.get("status") or "") == "queued" else 0,
                    str(r.get("created_at") or ""),
                ),
            )
        elif mode == "Newest First":
            items.sort(key=lambda r: str(r.get("updated_at") or r.get("created_at") or ""), reverse=True)
        elif mode == "Oldest First":
            items.sort(key=lambda r: str(r.get("created_at") or ""))
        elif mode == "Progress":
            items.sort(key=lambda r: int(r.get("progress") or 0), reverse=True)
        elif mode == "Name":
            items.sort(key=lambda r: project_display_name(r).lower())
        return items

    def apply_theme(self, _resolved: str = "") -> None:
        self._rebuild_rows()

    def _rebuild_rows(self) -> None:
        while self.list_layout.count() > 0:
            item = self.list_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
        self._cards.clear()

        clip_counts: dict[str, int] = {}
        for clip in self._clips:
            pid = str(clip.get("project_id") or "")
            clip_counts[pid] = clip_counts.get(pid, 0) + 1

        queued_ids = [
            str(r["id"])
            for r in sorted(
                (p for p in self._projects if p.get("status") == "queued"),
                key=lambda p: (int(p.get("queue_order") or 0), str(p.get("created_at") or "")),
            )
        ]

        visible = self._filtered_sorted_projects()
        self._visible_projects = visible
        visible_ids = {str(p["id"]) for p in visible}
        if self._selected_id and self._selected_id not in visible_ids:
            self._selected_id = None

        if not visible:
            empty = QLabel("No jobs match the current filter.")
            empty.setObjectName("muted")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty.setStyleSheet("padding: 20px 10px;")
            self.list_layout.addWidget(empty)
            self.list_layout.addStretch()
            self.selection_changed.emit()
            return

        for row in visible:
            pid = str(row["id"])
            q_idx = queued_ids.index(pid) if pid in queued_ids else -1
            card = JobRowCard(
                row,
                thumb_path=project_thumbnail_path(row, self._clips),
                clip_count=clip_counts.get(pid, 0),
                can_move_up=q_idx > 0,
                can_move_down=0 <= q_idx < len(queued_ids) - 1,
                selected=(pid == self._selected_id),
            )
            card.clicked.connect(self.select_project)
            card.action_triggered.connect(self.action_triggered.emit)
            self._cards[pid] = card
            self.list_layout.addWidget(card)
        self.list_layout.addStretch()
        self.list_container.adjustSize()
        self.list_container.updateGeometry()
        self.selection_changed.emit()

    def select_project(self, project_id: str | None) -> None:
        self._selected_id = project_id
        for pid, card in self._cards.items():
            card.set_selected(pid == project_id)
        self.selection_changed.emit()

    def select_row(self, index: int) -> None:
        if 0 <= index < len(self._visible_projects):
            self.select_project(str(self._visible_projects[index]["id"]))
        else:
            self.select_project(None)

    def selected_project(self) -> dict[str, Any] | None:
        if not self._selected_id:
            return None
        for row in self._visible_projects:
            if str(row["id"]) == self._selected_id:
                return row
        for row in self._projects:
            if str(row["id"]) == self._selected_id:
                return row
        return None


class CollapsibleLogsPanel(QFrame):
    """Collapsible Activity & Logs drawer collapsed by default."""

    def __init__(self, on_export, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("card")
        self._expanded = False

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 10, 14, 10)
        root.setSpacing(8)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)
        self.toggle_button = QPushButton("Activity && Logs", objectName="ghost")
        self.toggle_button.setFlat(True)
        self.toggle_button.setIcon(lucide_icon("chevron-right", theme_manager().tokens["text_secondary"], 14))
        self.toggle_button.setIconSize(QSize(14, 14))
        self.toggle_button.setStyleSheet("text-align: left; font-weight: 600;")
        self.toggle_button.clicked.connect(self.toggle)

        self.count_badge = QLabel("0 entries", objectName="helperText")

        header.addWidget(self.toggle_button, 1)
        header.addWidget(self.count_badge)
        root.addLayout(header)

        self.drawer = QWidget()
        drawer_lay = QVBoxLayout(self.drawer)
        drawer_lay.setContentsMargins(0, 4, 0, 0)
        drawer_lay.setSpacing(8)

        filter_row = QHBoxLayout()
        filter_row.setContentsMargins(0, 0, 0, 0)
        filter_row.setSpacing(8)
        self.level_combo = QComboBox()
        self.level_combo.addItems(["All", "INFO", "SUCCESS", "WARNING", "ERROR"])
        self.level_combo.currentTextChanged.connect(self._apply_filter)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Filter logs...")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.textChanged.connect(self._apply_filter)
        self.auto_scroll_check = QCheckBox("Auto-scroll")
        self.auto_scroll_check.setChecked(True)
        self.auto_scroll_check.toggled.connect(self._toggle_autoscroll)
        filter_row.addWidget(self.level_combo)
        filter_row.addWidget(self.search_input, 1)
        filter_row.addWidget(self.auto_scroll_check)
        drawer_lay.addLayout(filter_row)

        self.live_logs = LogView()
        self.live_logs.setMinimumHeight(140)
        self.live_logs.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        drawer_lay.addWidget(self.live_logs)

        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(8)
        self.copy_button = QPushButton("Copy Logs")
        self.clear_button = QPushButton("Clear Logs")
        self.export_button = QPushButton("Export Logs")
        self.copy_button.clicked.connect(self.live_logs.copy_all)
        self.clear_button.clicked.connect(self._clear_logs)
        self.export_button.clicked.connect(on_export)
        actions.addWidget(self.copy_button)
        actions.addWidget(self.clear_button)
        actions.addWidget(self.export_button)
        actions.addStretch()
        drawer_lay.addLayout(actions)

        self.drawer.setVisible(False)
        root.addWidget(self.drawer)

    def _apply_filter(self) -> None:
        self.live_logs.set_filter(level=self.level_combo.currentText(), search=self.search_input.text())

    def _toggle_autoscroll(self, enabled: bool) -> None:
        self.live_logs.auto_scroll = bool(enabled)

    def _clear_logs(self) -> None:
        self.live_logs.clear()
        self.refresh_count()

    def refresh_count(self) -> None:
        count = len(getattr(self.live_logs, "_entries", ()))
        self.count_badge.setText(f"{count} entr{'y' if count == 1 else 'ies'}")

    def toggle(self) -> None:
        self.set_expanded(not self._expanded)

    def apply_theme(self, _resolved: str = "") -> None:
        icon_name = "chevron-down" if self._expanded else "chevron-right"
        self.toggle_button.setIcon(lucide_icon(icon_name, theme_manager().tokens["text_secondary"], 14))

    def set_expanded(self, expanded: bool) -> None:
        self._expanded = bool(expanded)
        self.drawer.setVisible(self._expanded)
        self.apply_theme()
