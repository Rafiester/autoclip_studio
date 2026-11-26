from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import asdict
from pathlib import Path
from threading import Event
from typing import Callable

if sys.platform == "darwin":
    os.environ.setdefault("QT_MEDIA_BACKEND", "darwin")

from PySide6.QtCore import QFileInfo, QSignalBlocker, QSize, QThread, Qt, QUrl, Slot
from PySide6.QtGui import QCloseEvent, QDesktopServices, QIcon, QImage, QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QFileIconProvider, QFormLayout, QFrame, QGridLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QProgressBar, QPushButton, QScrollArea, QSizePolicy, QSpinBox,
    QSplitter, QStackedWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .captions import TEMPLATE_DEFAULT, TEMPLATES
from .clip_cleanup import delete_clips_transaction, open_clip_file, reveal_in_file_manager
from .config import SettingsStore, credential_store_name
from .database import Database
from .diagnostics import LOG, SUCCESS, log_error, redact
from .download_state import cleanup_workspace, download_coordinator, format_cooldown
from .media import DownloadProgress, is_youtube_url, youtube_id, youtube_source_error
from .models import ProjectOptions
from .paths import APP_ROOT, CACHE_DIR, OUTPUT_DIR, asset_path, ensure_directories
from .pipeline import Pipeline
from .providers import AIProvider, ProviderError
from .smart_reframe import REFRAME_MODES, REFRAME_STYLES
from .speech_engine import MODELS
from .subtitles import SrtValidationError, SrtValidationResult, parse_srt_file
from .thumbnails import export_thumbnail
from .transcription import SpeechProgress
from .ui.caption_library import CaptionLibrary
from .ui.clips_view import ExportThumbnailDialog, FramePickerDialog, ThumbnailTaskRunner
from .ui.clips_workspace import ClipDeleteDialog, ClipDetailPlayerPanel, ClipLibraryPanel
from .ui.dependency_view import DependencyManagerView
from .ui.download_view import DownloadView
from .ui.heatmap_view import AudienceHeatmapWidget
from .ui.icons import lucide_icon
from .ui.local_preview import LocalVideoDropZone, LocalVideoMetadata, LocalVideoPreview
from .ui.log_view import LogView
from .ui.performance_view import PerformanceView
from .ui.queue_workspace import (
    ActiveJobCard, CollapsibleLogsPanel, ErrorDetailsDialog, JobCompletedDialog, JobDetailsDialog,
    JobQueuePanel, QueueOverviewBar, parse_download_state, project_display_name, project_thumbnail_path,
)
from .ui.sidebar import (
    ALL_ROUTES_BY_INDEX, ALL_ROUTES_BY_KEY, LEGACY_ROUTE_ALIASES, FloatingSidebar, ThemePreviewCard,
)
from .ui.speech_view import SpeechProgressView, SpeechSettingsView
from .ui.style import STYLESHEET, SectionCard, configure_form_layout, configure_input, theme_manager
from .ui.youtube_preview import YouTubePreview
from .worker import FunctionThread, PipelineWorker
from .youtube_metadata import YouTubeMetadata


class MainWindow(QMainWindow):
    NAV_ITEMS = [
        "Dashboard", "New Project", "Processing Queue", "Generated Clips", "Caption Templates",
        "AI Providers", "Transcription", "Performance", "Dependencies", "General", "Appearance",
    ]

    def __init__(self) -> None:
        super().__init__()
        ensure_directories()
        self.settings = SettingsStore()
        self.database = Database()
        self.database.recover_interrupted_projects()
        self.pipeline = Pipeline(self.database, self.settings)
        self.worker_thread: QThread | None = None
        self.worker: PipelineWorker | None = None
        self.utility_threads: list[FunctionThread] = []
        self.utility_cancel = Event()
        self.utility_done: Callable[[object], None] | None = None
        self.speech_utility_active = False
        self.project_rows: list[dict] | None = None
        self.clip_rows: list[dict] | None = None
        self.queue_paused = False
        self._active_project_id: str | None = None
        self._completed_popups_shown: set[str] = set()
        self._active_completion_dialog: JobCompletedDialog | None = None
        self.closing = False
        self._hydrating_ui = True
        self._loading_provider_form = False
        self._model_load_seq = 0
        self.thumb_runner = ThumbnailTaskRunner(self.settings.performance().profile, self)
        self.thumb_runner.completed.connect(self._on_thumbnail_ready)
        self.thumb_runner.failed.connect(self._on_thumbnail_failed)
        self.setWindowTitle("AutoClip Studio")
        if (app := QApplication.instance()) is not None and not app.windowIcon().isNull():
            self.setWindowIcon(app.windowIcon())
        elif (icon_path := asset_path("autoclip.ico")).is_file():
            self.setWindowIcon(QIcon(str(icon_path)))
        ui_state = self.settings.ui_state()
        win_w = max(760, min(2560, int(ui_state.get("window_width") or 1280)))
        win_h = max(560, min(1600, int(ui_state.get("window_height") or 820)))
        self.resize(win_w, win_h)
        self.setMinimumSize(760, 560)
        self._theme = theme_manager()
        self._theme.apply(str(self.settings.data.get("appearance") or "dark"))
        self._build_ui()
        self._hydrating_ui = False
        self._theme.theme_changed.connect(self._on_theme_changed)
        self._on_theme_changed(self._theme.resolved)

    def _build_ui(self) -> None:
        root = QWidget()
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        ui_state = self.settings.ui_state()
        self.sidebar = FloatingSidebar(
            collapsed=bool(ui_state.get("sidebar_collapsed", False)),
            settings_expanded=bool(ui_state.get("settings_expanded", False)),
            last_settings_route=str(ui_state.get("last_settings_route") or "settings.general"),
        )
        self.sidebar.route_selected.connect(self.show_route)
        self.sidebar.collapsed_changed.connect(self._on_sidebar_collapsed_changed)
        self.sidebar.settings_expanded_changed.connect(self._on_settings_expanded_changed)
        self.brand_label = self.sidebar.brand_label
        self.nav_buttons: list[QPushButton] = self.sidebar.ordered_nav_buttons_by_page_index()
        self.appearance_buttons: dict[str, QPushButton] = {}

        # Inset gutter giving the floating sidebar crisp outer breathing room against the app canvas
        sidebar_gutter = QWidget()
        sidebar_gutter.setStyleSheet("background: transparent;")
        gutter_layout = QVBoxLayout(sidebar_gutter)
        gutter_layout.setContentsMargins(14, 14, 4, 14)
        gutter_layout.setSpacing(0)
        gutter_layout.addWidget(self.sidebar)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._dashboard_page())                     # 0: dashboard
        self.stack.addWidget(self._new_project_page())                   # 1: new_project
        self.stack.addWidget(self._queue_page())                         # 2: queue
        self.stack.addWidget(self._clips_page())                         # 3: clips
        self.stack.addWidget(self._templates_page())                     # 4: templates
        self.stack.addWidget(self._scroll_page(self._settings_page()))   # 5: settings.ai_providers
        self.stack.addWidget(self._scroll_page(self._speech_settings_page()))  # 6: settings.transcription
        self.stack.addWidget(self._scroll_page(self._performance_page()))      # 7: settings.performance
        self.stack.addWidget(self._scroll_page(self._dependencies_page()))     # 8: settings.dependencies
        self.stack.addWidget(self._scroll_page(self._general_settings_page())) # 9: settings.general
        self.stack.addWidget(self._scroll_page(self._appearance_settings_page()))  # 10: settings.appearance

        layout.addWidget(sidebar_gutter)
        layout.addWidget(self.stack, 1)
        self.setCentralWidget(root)
        self.show_page(0)

    def _on_sidebar_collapsed_changed(self, collapsed: bool) -> None:
        if hasattr(self, "sidebar_compact_check"):
            with QSignalBlocker(self.sidebar_compact_check):
                self.sidebar_compact_check.setChecked(bool(collapsed))
        if not self._hydrating_ui:
            self.settings.update_ui_state({"sidebar_collapsed": bool(collapsed)}, debounced=False)

    def _on_settings_expanded_changed(self, expanded: bool) -> None:
        if not self._hydrating_ui:
            self.settings.update_ui_state({"settings_expanded": bool(expanded)}, debounced=False)

    def set_appearance(self, mode: str) -> None:
        normalized = mode if mode in {"light", "dark", "system"} else "dark"
        self.settings.data["appearance"] = normalized
        self.settings.save()
        for mode_id, btn in self.appearance_buttons.items():
            with QSignalBlocker(btn):
                btn.setChecked(mode_id == normalized)
            if isinstance(btn, ThemePreviewCard):
                btn.apply_theme(self._theme.resolved)
        self._theme.apply(normalized)

    def _on_theme_changed(self, resolved: str) -> None:
        if hasattr(self, "sidebar"):
            self.sidebar.apply_theme(resolved)
        for btn in self.appearance_buttons.values():
            if isinstance(btn, ThemePreviewCard):
                btn.apply_theme(resolved)
        if hasattr(self, "live_logs"):
            self.live_logs.apply_theme(resolved)
        if hasattr(self, "heatmap_widget"):
            self.heatmap_widget.apply_theme(resolved)
        if hasattr(self, "dependency_view"):
            self.dependency_view.refresh_theme()
        if hasattr(self, "queue_overview"):
            self.queue_overview.apply_theme(resolved)
        if hasattr(self, "active_job_card"):
            self.active_job_card.apply_theme(resolved)
        if hasattr(self, "queue_panel"):
            self.queue_panel.apply_theme(resolved)
        if hasattr(self, "logs_panel"):
            self.logs_panel.apply_theme(resolved)
        if hasattr(self, "clips_library"):
            self.clips_library.apply_theme(resolved)
        if hasattr(self, "clip_detail"):
            self.clip_detail.apply_theme(resolved)

    def _page(self, title: str, subtitle: str) -> tuple[QWidget, QVBoxLayout]:
        outer = QWidget()
        page = QVBoxLayout(outer)
        page.setContentsMargins(24, 20, 24, 20)
        page.setSpacing(14)
        header = QVBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(4)
        header.addWidget(self._wrapping_label(title, object_name="pageTitle"))
        header.addWidget(self._wrapping_label(subtitle, object_name="muted"))
        page.addLayout(header)
        return outer, page

    @staticmethod
    def _wrapping_label(text: str, *, object_name: str = "") -> QLabel:
        label = QLabel(text, objectName=object_name)
        label.setWordWrap(True)
        label.setMinimumWidth(0)
        label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        return label

    @staticmethod
    def _scroll_page(content: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(content)
        return scroll

    def _dashboard_page(self) -> QWidget:
        outer, page = self._page("Dashboard", "Your local short-form production workspace")
        metrics = QHBoxLayout()
        metrics.setSpacing(12)
        self.metric_projects = self._metric_card("Projects", "0")
        self.metric_clips = self._metric_card("Generated clips", "0")
        self.metric_active = self._metric_card("Queue status", "Idle")
        for card in (self.metric_projects, self.metric_clips, self.metric_active):
            metrics.addWidget(card, 1)
        page.addLayout(metrics)
        action_card = QFrame(objectName="card")
        action_layout = QVBoxLayout(action_card)
        action_layout.setContentsMargins(20, 18, 20, 18)
        action_layout.setSpacing(10)
        action_title = self._wrapping_label("Create polished vertical clips from any long video", object_name="sectionTitle")
        action_layout.addWidget(action_title)
        action_layout.addWidget(self._wrapping_label(
            "Import locally or paste a YouTube link. Transcription, selection, framing, captions, and rendering run in one workflow.",
            object_name="muted",
        ))
        new_button = QPushButton("Start a new project", objectName="primary")
        new_button.clicked.connect(lambda: self.show_page(1))
        action_layout.addWidget(new_button, alignment=Qt.AlignmentFlag.AlignLeft)
        page.addWidget(action_card)
        page.addWidget(QLabel("Recent projects", objectName="sectionTitle"))
        self.recent_table = self._table(["Project", "Status", "Progress", "Created"])
        header = self.recent_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in (1, 2, 3):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        page.addWidget(self.recent_table, 1)
        return outer

    def _metric_card(self, label: str, value: str) -> QFrame:
        card = QFrame(objectName="card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.addWidget(self._wrapping_label(label, object_name="muted"))
        value_label = QLabel(value, objectName="metricValue")
        layout.addWidget(value_label)
        return card

    def _new_project_page(self) -> QWidget:
        outer, page = self._page("New Project", "Choose a source and configure your shorts")
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        form = QVBoxLayout(content)
        form.setContentsMargins(0, 0, 4, 8)
        form.setSpacing(16)
        source_group = SectionCard("Source video")
        source_layout = source_group.body_layout()

        # Compact source tabs: YouTube Link (default) | Local File
        tab_bar = QHBoxLayout()
        tab_bar.setSpacing(8)
        self.youtube_tab_btn = QPushButton("YouTube Link", objectName="sourceTab")
        self.youtube_tab_btn.setCheckable(True)
        self.youtube_tab_btn.setChecked(True)
        self.local_tab_btn = QPushButton("Local File", objectName="sourceTab")
        self.local_tab_btn.setCheckable(True)
        self.youtube_tab_btn.clicked.connect(lambda: self.set_source_tab(0))
        self.local_tab_btn.clicked.connect(lambda: self.set_source_tab(1))
        tab_bar.addWidget(self.youtube_tab_btn)
        tab_bar.addWidget(self.local_tab_btn)
        tab_bar.addStretch()
        source_layout.addLayout(tab_bar)

        self.source_stack = QStackedWidget()
        # Tab 0: YouTube Link (preserved)
        yt_pane = QWidget()
        yt_layout = QVBoxLayout(yt_pane)
        yt_layout.setContentsMargins(0, 4, 0, 0)
        yt_layout.setSpacing(8)
        self.source_input = QLineEdit()
        self.source_input.setAccessibleName("YouTube video URL")
        self.source_input.setPlaceholderText("Paste a YouTube video or Shorts URL…")
        yt_layout.addWidget(self.source_input)
        self.youtube_preview = YouTubePreview()
        self.youtube_preview.metadata_changed.connect(self._update_project_name)
        self.youtube_preview.idle.connect(self._finish_close)
        self.source_input.textChanged.connect(self.youtube_preview.set_source)
        yt_layout.addWidget(self.youtube_preview)
        self.heatmap_widget = AudienceHeatmapWidget()
        yt_layout.addWidget(self.heatmap_widget)
        self.use_yt_subtitles_check = QCheckBox("Use YouTube subtitles when available")
        self.use_yt_subtitles_check.setChecked(bool(self.settings.project_defaults().get("use_youtube_subtitles", False)))
        self.use_yt_subtitles_check.toggled.connect(self._persist_project_preferences)
        self.use_yt_subtitles_check.setToolTip(
            "Reuse manual YouTube subtitles or normalized automatic captions before running Whisper"
        )
        yt_layout.addWidget(self.use_yt_subtitles_check)
        self.yt_subtitles_hint = QLabel("", objectName="muted")
        self.yt_subtitles_hint.setWordWrap(True)
        self.yt_subtitles_hint.hide()
        yt_layout.addWidget(self.yt_subtitles_hint)
        self.source_stack.addWidget(yt_pane)

        # Tab 1: Local File
        local_pane = QWidget()
        local_layout = QVBoxLayout(local_pane)
        local_layout.setContentsMargins(0, 4, 0, 0)
        local_layout.setSpacing(8)
        self.local_drop_zone = LocalVideoDropZone()
        self.local_drop_zone.browse_requested.connect(self.browse_source)
        self.local_drop_zone.file_dropped.connect(self._set_local_source)
        local_layout.addWidget(self.local_drop_zone)
        self.local_source_input = QLineEdit()
        self.local_source_input.setAccessibleName("Local video file path")
        self.local_source_input.setPlaceholderText("Choose or drop a local MP4, MKV, MOV, WEBM, or AVI file…")
        self.local_source_input.textChanged.connect(self._on_local_text_changed)
        local_layout.addWidget(self.local_source_input)
        self.local_preview = LocalVideoPreview()
        self.local_preview.metadata_changed.connect(self._update_local_project_name)
        self.local_preview.replace_requested.connect(self.browse_source)
        self.local_preview.remove_requested.connect(lambda: self._set_local_source(""))
        self.local_preview.idle.connect(self._finish_close)
        local_layout.addWidget(self.local_preview)
        self.source_stack.addWidget(local_pane)
        source_layout.addWidget(self.source_stack)
        self.set_source_tab(0)

        # Compact optional SRT import
        self.import_srt_check = QCheckBox("Import existing subtitle (.srt)")
        self.import_srt_check.toggled.connect(self._toggle_srt_panel)
        source_layout.addWidget(self.import_srt_check)

        self.srt_panel = QFrame(objectName="summaryPill")
        srt_layout = QVBoxLayout(self.srt_panel)
        srt_layout.setContentsMargins(16, 12, 16, 12)
        srt_layout.setSpacing(8)
        srt_row = QHBoxLayout()
        srt_row.setContentsMargins(0, 0, 0, 0)
        srt_row.setSpacing(8)
        self.srt_input = QLineEdit()
        self.srt_input.setAccessibleName("Subtitle .srt file path")
        self.srt_input.setPlaceholderText("Choose an .srt subtitle file (UTF-8 / Indonesian / English)…")
        self.srt_input.textChanged.connect(self._validate_srt_inputs)
        srt_browse = QPushButton("Browse .srt")
        srt_browse.clicked.connect(self.browse_srt)
        self.srt_offset = QDoubleSpinBox()
        self.srt_offset.setRange(-60.0, 60.0)
        self.srt_offset.setSingleStep(0.25)
        self.srt_offset.setDecimals(2)
        self.srt_offset.setSuffix(" s")
        self.srt_offset.setToolTip("Optional subtitle timing offset in seconds")
        self.srt_offset.valueChanged.connect(self._validate_srt_inputs)
        srt_row.addWidget(self.srt_input, 1)
        srt_row.addWidget(srt_browse)
        srt_row.addWidget(QLabel("Offset:"))
        srt_row.addWidget(self.srt_offset)
        srt_layout.addLayout(srt_row)
        self.srt_status = QLabel("Select an .srt file to validate cues and language.", objectName="muted")
        self.srt_status.setWordWrap(True)
        srt_layout.addWidget(self.srt_status)
        self._srt_result: SrtValidationResult | None = None
        self._srt_error: str = ""
        self.srt_panel.hide()
        source_layout.addWidget(self.srt_panel)

        name_row = configure_form_layout(QFormLayout())
        self.project_name = configure_input(QLineEdit(placeholderText="Optional project name"))
        self._auto_project_name = ""
        name_row.addRow("Project name", self.project_name)
        name_row.labelForField(self.project_name).setObjectName("projectNameLabel")
        source_layout.addLayout(name_row)
        form.addWidget(source_group)
        proj_defaults = self.settings.project_defaults()
        settings_group = SectionCard("Clip settings")
        grid = QGridLayout()
        settings_group.body_layout().addLayout(grid)
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(12)
        self.clip_count = configure_input(self._spin(1, 10, int(proj_defaults.get("clip_count", 3))))
        self.min_duration = configure_input(self._spin(10, 120, int(proj_defaults.get("min_duration", 25)), " sec"))
        self.max_duration = configure_input(self._spin(15, 180, int(proj_defaults.get("max_duration", 60)), " sec"))
        self.aspect = configure_input(QComboBox(), min_contents_length=8); self.aspect.addItems(["9:16", "1:1", "4:5", "16:9"])
        self.aspect.setCurrentText(str(proj_defaults.get("aspect_ratio", "9:16")))
        self.resolution = configure_input(QComboBox(), min_contents_length=12); self.resolution.addItems(["1080x1920", "720x1280", "1080x1080", "1080x1350", "1920x1080"])
        self.resolution.setCurrentText(str(proj_defaults.get("resolution", "1080x1920")))
        self.aspect.currentTextChanged.connect(self._aspect_changed)
        self.clip_count.valueChanged.connect(self._persist_project_preferences)
        self.min_duration.valueChanged.connect(self._persist_project_preferences)
        self.max_duration.valueChanged.connect(self._persist_project_preferences)
        self.resolution.currentTextChanged.connect(self._persist_project_preferences)
        grid.addWidget(QLabel("Number of clips"), 0, 0); grid.addWidget(self.clip_count, 0, 1)
        grid.addWidget(QLabel("Minimum duration"), 0, 2); grid.addWidget(self.min_duration, 0, 3)
        grid.addWidget(QLabel("Maximum duration"), 1, 0); grid.addWidget(self.max_duration, 1, 1)
        grid.addWidget(QLabel("Aspect ratio"), 1, 2); grid.addWidget(self.aspect, 1, 3)
        grid.addWidget(QLabel("Resolution"), 2, 0); grid.addWidget(self.resolution, 2, 1)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        form.addWidget(settings_group)
        automation = SectionCard("Automation")
        auto_form = configure_form_layout(QFormLayout())
        automation.body_layout().addLayout(auto_form)
        self.reframe_combo = configure_input(QComboBox())
        for code, label in REFRAME_MODES:
            self.reframe_combo.addItem(label, code)
        self.reframe_combo.setToolTip(
            "Auto Smart Reframe keeps single-speaker 9:16 portrait framing by default.\n"
            "Select Podcast Split Screen manually if you want a stacked two-host layout."
        )
        saved_reframe = str(proj_defaults.get("reframe_mode", "auto"))
        reframe_idx = self.reframe_combo.findData(saved_reframe)
        self.reframe_combo.setCurrentIndex(reframe_idx if reframe_idx >= 0 else 0)
        self.reframe_style_combo = configure_input(QComboBox())
        for code, label in REFRAME_STYLES:
            self.reframe_style_combo.addItem(label, code)
        saved_style = str(proj_defaults.get("reframe_style", "natural"))
        style_idx = self.reframe_style_combo.findData(saved_style)
        self.reframe_style_combo.setCurrentIndex(style_idx if style_idx >= 0 else 0)
        self.reframe_style_combo.setToolTip(
            "Natural: balanced editorial switching · Stable: longer holds, fewer cuts · Dynamic: faster turns"
        )
        self.reframe_style_combo.setEnabled((self.reframe_combo.currentData() or "auto") not in ("off", "fit_original"))
        self.reframe_style_combo.currentIndexChanged.connect(self._persist_project_preferences)
        self.face_tracking = QCheckBox("Auto face tracking")
        self.face_tracking.setChecked((self.reframe_combo.currentData() or "auto") != "off")
        self.face_tracking.hide()  # Preserved for API compatibility; synchronized with Smart Reframing selector
        self.reframe_combo.currentIndexChanged.connect(self._on_reframe_mode_changed)
        self.captions = QCheckBox("Animated word captions"); self.captions.setChecked(bool(proj_defaults.get("captions", True)))
        self.cache = QCheckBox("Cache downloads and transcripts"); self.cache.setChecked(bool(proj_defaults.get("cache_enabled", self.settings.data.get("cache_enabled", True))))
        self.captions.toggled.connect(self._persist_project_preferences)
        self.cache.toggled.connect(self._persist_project_preferences)
        self.template_combo = configure_input(QComboBox())
        self.template_combo.addItems(TEMPLATES.keys())
        self.template_combo.setCurrentText(self.default_template())
        self.template_combo.currentTextChanged.connect(self._on_project_template_changed)
        options_row = QHBoxLayout()
        options_row.setContentsMargins(0, 0, 0, 0)
        options_row.setSpacing(20)
        options_row.addWidget(self.captions)
        options_row.addWidget(self.cache)
        options_row.addStretch()
        auto_form.addRow("Smart reframing", self.reframe_combo)
        auto_form.addRow("Reframing style", self.reframe_style_combo)
        auto_form.addRow("Caption template", self.template_combo)
        auto_form.addRow("Options", options_row)
        form.addWidget(automation)
        ai_group = SectionCard("AI and transcription")
        ai_form = configure_form_layout(QFormLayout())
        ai_group.body_layout().addLayout(ai_form)
        self.highlight_mode_combo = configure_input(QComboBox())
        self.highlight_mode_combo.addItem("Hybrid (AI + Most Replayed)", "hybrid")
        self.highlight_mode_combo.addItem("AI Analysis", "ai")
        self.highlight_mode_combo.addItem("Most Replayed", "most_replayed")
        saved_hl_mode = str(proj_defaults.get("highlight_mode", "ai"))
        hl_idx = self.highlight_mode_combo.findData(saved_hl_mode)
        self.highlight_mode_combo.setCurrentIndex(hl_idx if hl_idx >= 0 else 1)
        self.highlight_mode_combo.currentIndexChanged.connect(self._on_highlight_mode_changed)
        self.highlight_mode_note = QLabel("", objectName="muted")
        self.highlight_mode_note.setWordWrap(True)
        self.highlight_mode_note.hide()
        saved_ai_weight = max(0, min(100, int(proj_defaults.get("ai_weight_pct", 60))))
        self.hybrid_weight_spin = configure_input(self._spin(0, 100, saved_ai_weight, f"% AI / {100 - saved_ai_weight}% Replay"))
        self.hybrid_weight_spin.setToolTip("Adjust Hybrid ranking weight between AI content score and audience replay score")
        self.hybrid_weight_spin.valueChanged.connect(self._on_hybrid_weight_changed)
        self.project_provider = configure_input(QComboBox()); self.project_provider.addItems(["gemini", "openai", "custom"])
        self.project_provider.setCurrentText(self.settings.data["active_provider"])
        self.project_model = configure_input(QLineEdit(self.settings.provider().get("model", "")))
        self.project_model.editingFinished.connect(self._on_project_model_edited)
        self.multimodal_check = QCheckBox("Optional Gemini video analysis (uploads source)")
        self.multimodal_check.setChecked(bool(proj_defaults.get("multimodal", False)) and self.project_provider.currentText() == "gemini")
        self.multimodal_check.setEnabled(self.project_provider.currentText() == "gemini")
        self.multimodal_check.toggled.connect(self._persist_project_preferences)
        self.whisper_model = configure_input(QComboBox()); self.whisper_model.addItems(MODELS); self.whisper_model.setCurrentText(self.settings.data["whisper_model"])
        self.whisper_device = configure_input(QComboBox()); self.whisper_device.addItems(["auto", "cuda", "cpu"]); self.whisper_device.setCurrentText(self.settings.data["whisper_device"])
        self.whisper_model.currentTextChanged.connect(self._on_project_whisper_changed)
        self.whisper_device.currentTextChanged.connect(self._on_project_whisper_changed)
        self.project_provider.currentTextChanged.connect(self._project_provider_changed)
        ai_form.addRow("Highlight detection", self.highlight_mode_combo)
        ai_form.addRow("", self.highlight_mode_note)
        ai_form.addRow("Hybrid weighting", self.hybrid_weight_spin)
        ai_form.addRow("Provider", self.project_provider)
        ai_form.addRow("Model", self.project_model)
        ai_form.addRow("Multimodal", self.multimodal_check)
        ai_form.addRow("Whisper model", self.whisper_model)
        ai_form.addRow("Whisper device", self.whisper_device)
        self._on_highlight_mode_changed()
        form.addWidget(ai_group)
        actions = QHBoxLayout()
        self.generate_button = QPushButton("Generate Shorts", objectName="primary")
        self.generate_button.clicked.connect(self.generate)
        actions.addStretch(); actions.addWidget(self.generate_button)
        form.addLayout(actions)
        form.addStretch()
        scroll.setWidget(content)
        page.addWidget(scroll, 1)
        return outer

    def _queue_page(self) -> QWidget:
        outer, page = self._page(
            "Processing Queue",
            "Monitor active media rendering, queued jobs, pipeline progress, and activity logs",
        )
        page.setSpacing(12)

        self.queue_overview = QueueOverviewBar()
        self.queue_overview.pause_toggled.connect(self.toggle_queue_paused)
        page.addWidget(self.queue_overview)

        self.active_job_card = ActiveJobCard(self.cancel_processing, self.pause_processing)
        self.active_job_card.new_project_requested.connect(lambda: self.show_page(1))
        page.addWidget(self.active_job_card)

        self.processing_status_row = self.active_job_card.processing_status_row
        self.processing_progress_row = self.active_job_card.processing_progress_row
        self.queue_status = self.active_job_card.queue_status
        self.pause_button = self.active_job_card.pause_button
        self.cancel_button = self.active_job_card.cancel_button
        self.progress_bar = self.active_job_card.progress_bar
        self.progress_text = self.active_job_card.progress_text
        self.download_view = self.active_job_card.download_view
        self.speech_progress = self.active_job_card.speech_progress

        self.queue_panel = JobQueuePanel()
        self.queue_panel.selection_changed.connect(self._update_retry_button)
        self.queue_panel.action_triggered.connect(self._handle_queue_action)
        self.retry_analysis_button = self.queue_panel.retry_analysis_button
        self.retry_analysis_button.clicked.connect(self.retry_analysis)
        page.addWidget(self.queue_panel, 1)

        self.logs_panel = CollapsibleLogsPanel(self.export_logs)
        self.live_logs = self.logs_panel.live_logs
        page.addWidget(self.logs_panel)

        # Responsive scroll wrapper: stretches Job Queue & History to fill tall viewports (no outer scrollbar),
        # while preventing any vertical text crushing/overlap on compact windows or when Logs are expanded!
        return self._scroll_page(outer)

    def export_logs(self) -> None:
        filename, _ = QFileDialog.getSaveFileName(self, "Export logs", "autoclip-logs.txt", "Text files (*.txt)")
        if not filename:
            return
        path = Path(filename)
        if not path.suffix:
            path = path.with_suffix(".txt")
        try:
            path.write_text(self.live_logs.log_text(), encoding="utf-8")
        except OSError as error:
            QMessageBox.warning(self, "Log export failed", redact(str(error)))

    def _clips_page(self) -> QWidget:
        outer, page = self._page("Generated Clips", "Inspect, preview, and manage your exported shorts")
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(8)
        self.clips_library = ClipLibraryPanel()
        self.clips_library.clip_selected.connect(self._on_clip_selected)
        self.clips_library.export_all_requested.connect(self.export_all_clips)
        self.clips_library.open_clip_requested.connect(self._open_clip_item)
        self.clips_library.open_clip_folder_requested.connect(self._open_clip_folder_item)
        self.clips_library.delete_clips_requested.connect(self._confirm_and_delete_clips)
        splitter.addWidget(self.clips_library)

        self.clip_detail = ClipDetailPlayerPanel()
        self.player = self.clip_detail.player
        ui_state = self.settings.ui_state()
        saved_vol = int(ui_state.get("player_volume", 80))
        saved_muted = bool(ui_state.get("player_muted", False))
        self.clip_detail.set_audio_preferences(saved_vol, saved_muted)
        self.clip_detail.audio_settings_changed.connect(self._on_player_audio_settings_changed)
        self.clip_detail.open_file_requested.connect(self.open_selected_clip)
        self.clip_detail.open_folder_requested.connect(self.open_selected_clip_folder)
        self.clip_detail.regenerate_thumb_requested.connect(self.regenerate_thumbnail)
        self.clip_detail.select_frame_requested.connect(self.select_thumbnail_frame)
        self.clip_detail.export_thumb_requested.connect(self.export_selected_thumbnail)
        self.clip_detail.rename_clip_requested.connect(self.rename_selected_clip)
        self.clip_detail.delete_clip_requested.connect(self.delete_selected_clip)
        self.clip_detail.prev_clip_requested.connect(lambda: self.clips_library.select_relative(-1))
        self.clip_detail.next_clip_requested.connect(lambda: self.clips_library.select_relative(1))
        splitter.addWidget(self.clip_detail)
        splitter.setStretchFactor(0, 5)
        splitter.setStretchFactor(1, 4)
        page.addWidget(splitter, 1)
        return outer

    def _templates_page(self) -> QWidget:
        outer, page = self._page("Caption Templates", "Preview animated captions and choose a default for new projects.")
        sync_mode, offset_ms = self.settings.caption_timing()
        y_ratio, avoid_faces, show_safe, safe_platform = self.settings.caption_positioning()
        self.caption_library = CaptionLibrary(
            self.default_template(),
            self.choose_template,
            lambda: self.show_page(1),
            sync_mode=sync_mode,
            offset_ms=offset_ms,
            on_timing_changed=self._on_caption_timing_changed,
            y_ratio=y_ratio,
            avoid_faces=avoid_faces,
            show_safe_area=show_safe,
            safe_platform=safe_platform,
            on_positioning_changed=self._on_caption_positioning_changed,
        )
        self.caption_library.idle.connect(self._finish_close)
        page.addWidget(self.caption_library, 1)
        return outer

    def _settings_page(self) -> QWidget:
        outer, page = self._page("AI Providers", f"Credentials are stored securely in {credential_store_name()}")
        group = self.provider_group = SectionCard("Provider connection")
        form = configure_form_layout(QFormLayout())
        group.body_layout().addLayout(form)
        self.settings_provider = configure_input(QComboBox()); self.settings_provider.addItems(["gemini", "openai", "custom"])
        self.settings_provider.setCurrentText(self.settings.data["active_provider"])
        self.provider_name_input = configure_input(QLineEdit())
        self.provider_name_input.setPlaceholderText("Display name (e.g. OpenRouter, Local vLLM)")
        self.api_key_input = configure_input(QLineEdit()); self.api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.base_url_input = configure_input(QLineEdit())
        self.model_input = configure_input(QComboBox(), min_contents_length=24); self.model_input.setEditable(True)
        self.model_input.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        if (edit := self.model_input.lineEdit()) is not None:
            edit.setPlaceholderText("Select from list or type model ID manually")
            edit.editingFinished.connect(self._on_settings_model_committed)
        self.model_input.activated.connect(lambda _idx: self._on_settings_model_committed())
        self.fallback_models_input = configure_input(QLineEdit())
        self.fallback_models_input.setPlaceholderText("Comma-separated model IDs, in retry order")
        self.auto_fallback_check = QCheckBox("Automatically try fallback models")
        self.auto_fallback_check.toggled.connect(self.fallback_models_input.setEnabled)
        self.settings_provider.currentTextChanged.connect(self.load_provider_form)
        form.addRow("Provider", self.settings_provider)
        form.addRow("Provider name", self.provider_name_input)
        form.addRow("API key", self.api_key_input)
        form.addRow("Base URL", self.base_url_input)
        form.addRow("Model", self.model_input)
        form.addRow("Fallback models", self.fallback_models_input)
        form.addRow(self.auto_fallback_check)
        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        save = QPushButton("Save settings", objectName="primary"); save.clicked.connect(self.save_provider)
        test = QPushButton("Test connection"); test.clicked.connect(self.test_provider)
        models = QPushButton("Load models"); models.clicked.connect(self.load_models)
        buttons.addWidget(save)
        buttons.addWidget(test)
        buttons.addWidget(models)
        buttons.addStretch()
        form.addRow(buttons)
        page.addWidget(group)
        self.connection_status = self._wrapping_label("", object_name="muted")
        self.connection_status.setTextFormat(Qt.TextFormat.PlainText)
        page.addWidget(self.connection_status)
        page.addStretch()
        self.load_provider_form(self.settings_provider.currentText())
        return outer

    def _general_settings_page(self) -> QWidget:
        outer, page = self._page(
            "General",
            "Configure default export paths, hardware encoding, caching, and application preferences",
        )
        local = SectionCard("Storage & encoding")
        local_form = configure_form_layout(QFormLayout())
        local.body_layout().addLayout(local_form)
        self.output_input = configure_input(QLineEdit(self.settings.data.get("output_dir", "")))
        self.output_input.setPlaceholderText(str(OUTPUT_DIR))
        self.output_input.setAccessibleName("Output folder")
        output_row = QHBoxLayout(); output_row.setSpacing(8); output_row.addWidget(self.output_input, 1)
        output_browse = QPushButton("Browse"); output_browse.clicked.connect(self.browse_output); output_row.addWidget(output_browse)
        hw_label = "Use Apple VideoToolbox when available" if sys.platform == "darwin" else "Use NVENC when available"
        self.hardware_check = QCheckBox(hw_label)
        self.hardware_check.setChecked(bool(self.settings.data.get("hardware_encoding", True)))
        local_form.addRow("Output folder", output_row)
        local_form.addRow("Encoding", self.hardware_check)
        page.addWidget(local)

        cache_card = SectionCard("Project defaults & cache")
        cache_form = configure_form_layout(QFormLayout())
        cache_card.body_layout().addLayout(cache_form)
        self.general_cache_check = QCheckBox("Reuse cached downloads, transcripts, and tracking data")
        self.general_cache_check.setChecked(bool(self.settings.project_defaults().get("cache_enabled", True)))
        cache_form.addRow("Cache", self.general_cache_check)

        cache_row = QHBoxLayout(); cache_row.setSpacing(10)
        self.cache_size_label = QLabel("Calculating…", objectName="muted")
        clear_cache_btn = QPushButton("Clear temporary cache")
        clear_cache_btn.clicked.connect(self._clear_temporary_cache)
        cache_row.addWidget(self.cache_size_label, 1)
        cache_row.addWidget(clear_cache_btn)
        cache_form.addRow("Cache footprint", cache_row)

        actions = QHBoxLayout(); actions.setSpacing(10)
        save_local = QPushButton("Save general settings", objectName="primary")
        save_local.clicked.connect(self.save_local_settings)
        reset_settings = QPushButton("Reset to defaults")
        reset_settings.clicked.connect(self.reset_all_settings)
        actions.addWidget(save_local)
        actions.addWidget(reset_settings)
        actions.addStretch()
        cache_form.addRow(actions)
        page.addWidget(cache_card)

        self.general_status = self._wrapping_label("", object_name="muted")
        self.general_status.setTextFormat(Qt.TextFormat.PlainText)
        page.addWidget(self.general_status)
        page.addStretch()
        self._refresh_cache_footprint()
        return outer

    def _appearance_settings_page(self) -> QWidget:
        outer, page = self._page(
            "Appearance",
            "Customize application theme and sidebar layout — changes apply immediately",
        )
        theme_card = SectionCard("Theme")
        theme_lay = theme_card.body_layout()
        theme_lay.addWidget(self._wrapping_label(
            "Choose a light, dark, or OS-synchronized appearance across the entire workspace.",
            object_name="muted",
        ))
        grid = QGridLayout()
        grid.setContentsMargins(0, 4, 0, 0)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(12)
        current_appearance = str(self.settings.data.get("appearance") or "dark")
        specs = (
            ("light", "Light", "Bright canvas & crisp contrast"),
            ("dark", "Dark", "Low-glare studio workspace"),
            ("system", "System", "Match operating system"),
        )
        for col, (mode_id, title, sub) in enumerate(specs):
            card = ThemePreviewCard(mode_id, title, sub)
            card.setChecked(current_appearance == mode_id)
            card.clicked.connect(lambda _c=False, m=mode_id: self.set_appearance(m))
            self.appearance_buttons[mode_id] = card
            grid.addWidget(card, 0, col)
            grid.setColumnStretch(col, 1)
        theme_lay.addLayout(grid)
        page.addWidget(theme_card)

        nav_card = SectionCard("Navigation")
        nav_form = configure_form_layout(QFormLayout())
        nav_card.body_layout().addLayout(nav_form)
        self.sidebar_compact_check = QCheckBox("Use collapsed icon-only floating sidebar")
        self.sidebar_compact_check.setChecked(self.sidebar.collapsed)
        self.sidebar_compact_check.toggled.connect(lambda checked: self.sidebar.set_collapsed(checked))
        nav_form.addRow("Sidebar mode", self.sidebar_compact_check)
        page.addWidget(nav_card)
        page.addStretch()
        return outer

    def _refresh_cache_footprint(self) -> None:
        if not hasattr(self, "cache_size_label"):
            return
        total = 0
        if CACHE_DIR.is_dir():
            for path in CACHE_DIR.rglob("*"):
                if path.is_file():
                    try:
                        total += path.stat().st_size
                    except OSError:
                        pass
        mb = total / (1024 * 1024)
        self.cache_size_label.setText(f"{mb:.1f} MB in local cache" if mb >= 1.0 else f"{total / 1024:.0f} KB in local cache")

    def _clear_temporary_cache(self) -> None:
        if self.worker_thread is not None:
            self.general_status.setText("Wait for active processing to finish before clearing cache.")
            return
        for sub in ("jobs", "youtube-metadata", "youtube-subtitles"):
            shutil.rmtree(CACHE_DIR / sub, ignore_errors=True)
        self._refresh_cache_footprint()
        self.general_status.setText("Temporary job and metadata caches cleared (saved clips and settings preserved).")

    @staticmethod
    def _spin(minimum: int, maximum: int, value: int, suffix: str = "") -> QSpinBox:
        widget = QSpinBox(); widget.setRange(minimum, maximum); widget.setValue(value); widget.setSuffix(suffix)
        return widget

    @staticmethod
    def _table(headers: list[str]) -> QTableWidget:
        table = QTableWidget(0, len(headers)); table.setHorizontalHeaderLabels(headers)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setAlternatingRowColors(True); table.verticalHeader().setVisible(False)
        return table

    def _performance_page(self) -> QWidget:
        outer, page = self._page("Performance", "Adaptive resource profiles, thermal protection, and live hardware telemetry")
        self.performance_view = PerformanceView(
            self.pipeline.monitor,
            self.settings.performance(),
            self._save_performance_settings,
        )
        page.addWidget(self.performance_view, 1)
        return outer

    def _dependencies_page(self) -> QWidget:
        outer, page = self._page("Dependency Manager", "Inspect, repair, update yt-dlp, and manage runtimes and models")
        self.dependency_view = DependencyManagerView(
            self.settings.speech,
            is_job_active=lambda: self.worker_thread is not None,
        )
        page.addWidget(self.dependency_view, 1)
        return outer

    def _save_performance_settings(self, perf) -> None:
        self.settings.data["performance"] = asdict(perf)
        self.settings.save()

    def _on_player_audio_settings_changed(self, volume_pct: int, muted: bool) -> None:
        if self._hydrating_ui:
            return
        self.settings.update_ui_state({
            "player_volume": max(0, min(100, int(volume_pct))),
            "player_muted": bool(muted),
        }, debounced=True)

    def show_route(self, route: str) -> None:
        """Navigate by canonical route key or legacy page name (e.g. 'Dependencies', 'AI Provider Settings')."""
        normalized = str(route or "").strip()
        key = normalized if normalized in ALL_ROUTES_BY_KEY else LEGACY_ROUTE_ALIASES.get(normalized.lower(), "")
        spec = ALL_ROUTES_BY_KEY.get(key)
        if spec is not None:
            self.show_page(spec.page_index)

    def show_page(self, index: int | str) -> None:
        if isinstance(index, str):
            self.show_route(index)
            return
        if self.stack.currentIndex() == 3 and index != 3 and hasattr(self, "clip_detail"):
            self.clip_detail.release_media()
        if self.stack.currentIndex() == 4 and index != 4:
            self.caption_library.stop()
        if hasattr(self, "performance_view"):
            self.performance_view.set_polling(index == 7)
        self.stack.setCurrentIndex(index)
        spec = ALL_ROUTES_BY_INDEX.get(index)
        if spec is not None and hasattr(self, "sidebar"):
            self.sidebar.set_active_route(spec.key)
            if spec.key.startswith("settings.") and not self._hydrating_ui:
                self.settings.update_ui_state({"last_settings_route": spec.key}, debounced=True)
        else:
            for button_index, button in enumerate(self.nav_buttons):
                button.setChecked(button_index == index)
        if index in {0, 2, 3}:
            self.refresh_all()
            if index == 3 and hasattr(self, "clips_library"):
                self.clips_library.refresh_file_availability()
                self.preview_selected()
        elif index == 9:
            self._refresh_cache_footprint()

    def set_source_tab(self, index: int) -> None:
        tab = 1 if index == 1 else 0
        self.youtube_tab_btn.setChecked(tab == 0)
        self.local_tab_btn.setChecked(tab == 1)
        self.source_stack.setCurrentIndex(tab)
        for i in range(self.source_stack.count()):
            page = self.source_stack.widget(i)
            policy = QSizePolicy.Policy.Preferred if i == tab else QSizePolicy.Policy.Ignored
            page.setSizePolicy(QSizePolicy.Policy.Expanding, policy)
        self.source_stack.adjustSize()
        if hasattr(self, "highlight_mode_combo"):
            self._sync_highlight_mode_availability()

    def browse_source(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose source video", "",
            "Videos (*.mp4 *.mkv *.mov *.webm *.avi *.m4v);;All files (*)",
        )
        if path:
            self.set_source_tab(1)
            self._set_local_source(path)

    def _set_local_source(self, path: str) -> None:
        self.local_source_input.setText(path)
        if not path:
            self.local_preview.clear()

    @Slot(str)
    def _on_local_text_changed(self, text: str) -> None:
        self.local_preview.set_file(text)

    @Slot(object)
    def _update_local_project_name(self, metadata: LocalVideoMetadata | None) -> None:
        suggestion = metadata.path.stem if metadata else ""
        if self.project_name.text() in ("", self._auto_project_name):
            self.project_name.setText(suggestion)
        self._auto_project_name = suggestion

    def _toggle_srt_panel(self, checked: bool) -> None:
        self.srt_panel.setVisible(checked)
        if checked:
            self._validate_srt_inputs()
        else:
            self._srt_result = None
            self._srt_error = ""

    def browse_srt(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose subtitle file", "", "SubRip Subtitles (*.srt);;All files (*)",
        )
        if path:
            self.srt_input.setText(path)

    def _validate_srt_inputs(self) -> None:
        if not self.import_srt_check.isChecked():
            self._srt_result = None
            self._srt_error = ""
            return
        raw_path = self.srt_input.text().strip()
        t = theme_manager().tokens
        if not raw_path:
            self._srt_result = None
            self._srt_error = "Choose an .srt file or uncheck 'Import existing subtitle (.srt)'."
            self.srt_status.setStyleSheet(f"color: {t['text_secondary']};")
            self.srt_status.setText("Select an .srt file to validate cues and language.")
            return
        try:
            result = parse_srt_file(Path(raw_path), offset_seconds=self.srt_offset.value())
            self._srt_result = result
            self._srt_error = ""
            self.srt_status.setStyleSheet(f"color: {t['semantic_success']};")
            self.srt_status.setText(
                f"✓ Valid SRT: {Path(raw_path).name}  •  {result.cue_count} cues  •  "
                f"{result.language_label}  •  {result.encoding}"
            )
        except SrtValidationError as error:
            self._srt_result = None
            self._srt_error = str(error)
            self.srt_status.setStyleSheet(f"color: {t['semantic_danger_fg']};")
            self.srt_status.setText(f"Invalid SRT: {error}")

    def browse_output(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Choose output folder", self.output_input.text() or str(OUTPUT_DIR))
        if path: self.output_input.setText(path)

    def _on_hybrid_weight_changed(self, ai_pct: int) -> None:
        replay_pct = max(0, 100 - int(ai_pct))
        self.hybrid_weight_spin.setSuffix(f"% AI / {replay_pct}% Replay")
        self._persist_project_preferences()

    def _on_highlight_mode_changed(self) -> None:
        mode = self.highlight_mode_combo.currentData() or "ai"
        self.hybrid_weight_spin.setEnabled(mode == "hybrid")
        if hasattr(self, "heatmap_widget"):
            self.heatmap_widget.set_mode_label(self.highlight_mode_combo.currentText())
        self._sync_highlight_mode_availability()
        self._persist_project_preferences()

    def _sync_highlight_mode_availability(self) -> None:
        using_local = self.source_stack.currentIndex() == 1
        metadata = (getattr(self, "_latest_youtube_metadata", None) or self.youtube_preview.result) if not using_local else None
        has_heatmap = bool(metadata and metadata.has_heatmap)
        selected_mode = self.highlight_mode_combo.currentData() or "ai"
        if using_local:
            if selected_mode != "ai":
                with QSignalBlocker(self.highlight_mode_combo):
                    self.highlight_mode_combo.setCurrentIndex(self.highlight_mode_combo.findData("ai"))
            self.highlight_mode_note.setText("Local video selected — using AI Analysis (YouTube heatmap unavailable).")
            self.highlight_mode_note.show()
            self.hybrid_weight_spin.setEnabled(False)
            return
        if metadata is not None and not has_heatmap:
            if selected_mode != "ai":
                with QSignalBlocker(self.highlight_mode_combo):
                    self.highlight_mode_combo.setCurrentIndex(self.highlight_mode_combo.findData("ai"))
            self.highlight_mode_note.setText("YouTube audience heatmap is unavailable for this video — falling back to AI Analysis.")
            self.highlight_mode_note.show()
            self.hybrid_weight_spin.setEnabled(False)
            return
        self.highlight_mode_note.hide()
        self.hybrid_weight_spin.setEnabled((self.highlight_mode_combo.currentData() or "ai") == "hybrid")

    @Slot(object)
    def _update_project_name(self, metadata: YouTubeMetadata | None) -> None:
        self._latest_youtube_metadata = metadata
        suggestion = metadata.title if metadata else ""
        if self.project_name.text() in ("", self._auto_project_name):
            self.project_name.setText(suggestion)
        self._auto_project_name = suggestion
        if metadata is None:
            self.heatmap_widget.clear()
            self.yt_subtitles_hint.hide()
            self._sync_highlight_mode_availability()
            return
        has_heatmap = self.heatmap_widget.set_heatmap(
            metadata.heatmap,
            duration=metadata.duration,
            min_duration=self.min_duration.value(),
            max_duration=self.max_duration.value(),
            mode_text="Hybrid (AI + Most Replayed)" if metadata.has_heatmap else "AI Analysis",
        )
        # Default to Hybrid when heatmap data is available; fallback to AI otherwise.
        target_mode = "hybrid" if has_heatmap else "ai"
        with QSignalBlocker(self.highlight_mode_combo):
            idx = self.highlight_mode_combo.findData(target_mode)
            if idx >= 0:
                self.highlight_mode_combo.setCurrentIndex(idx)
        self.heatmap_widget.set_mode_label(self.highlight_mode_combo.currentText())
        if metadata.has_subtitles:
            parts = []
            if metadata.subtitle_languages:
                parts.append(f"Manual: {', '.join(metadata.subtitle_languages[:6])}")
            if metadata.auto_caption_languages:
                parts.append(f"Auto: {', '.join(metadata.auto_caption_languages[:6])}")
            self.yt_subtitles_hint.setText("Available YouTube tracks — " + "  •  ".join(parts))
            self.yt_subtitles_hint.show()
        else:
            self.yt_subtitles_hint.setText("No YouTube subtitles detected; Speech-to-Text will be used.")
            self.yt_subtitles_hint.show()
        self._sync_highlight_mode_availability()

    def generate(self) -> None:
        if self.speech_utility_active:
            QMessageBox.information(self, "Speech task active", "Wait for the current speech task to finish first.")
            return
        using_local = self.source_stack.currentIndex() == 1
        source = (self.local_source_input.text() if using_local else self.source_input.text()).strip()
        youtube = is_youtube_url(source)
        error = ""
        if using_local:
            if not source or not Path(source).is_file():
                error = "Choose an existing local video file (MP4, MKV, MOV, WEBM, AVI)."
        else:
            error = youtube_source_error(source) if youtube else ""
            if not youtube and (not source or not Path(source).is_file()):
                error = "Enter a valid YouTube URL or switch to the Local File tab."
        if error:
            QMessageBox.warning(self, "Invalid source", error)
            return
        if self.min_duration.value() > self.max_duration.value():
            QMessageBox.warning(self, "Invalid duration", "Minimum duration cannot exceed maximum duration.")
            return

        srt_path = ""
        srt_offset = 0.0
        if self.import_srt_check.isChecked():
            self._validate_srt_inputs()
            if self._srt_error or self._srt_result is None:
                QMessageBox.warning(self, "Invalid subtitle file", self._srt_error or "Choose a valid .srt file.")
                return
            srt_path = str(Path(self.srt_input.text().strip()).resolve())
            srt_offset = float(self.srt_offset.value())

        metadata = self.youtube_preview.result if not using_local else None
        if metadata and (not youtube or metadata.video_id != youtube_id(source)):
            metadata = None
        local_meta = self.local_preview.result if using_local else None
        default_name = (metadata.title if metadata else (local_meta.path.stem if local_meta else (Path(source).stem if not youtube else "")))
        reframe_mode = str(self.reframe_combo.currentData() or "auto")
        reframe_style = str(self.reframe_style_combo.currentData() or "natural")
        provider_name = self.project_provider.currentText()
        resolved_cfg = self.settings.resolve_provider_config(provider_name, model_override=self.project_model.text().strip())
        if resolved_cfg["api_key"] and not resolved_cfg["model"]:
            QMessageBox.warning(
                self, "AI model required",
                f"No model ID is selected for the '{provider_name}' provider.\n\n"
                "Open AI Provider Settings, select or type a model ID, and click Save settings.",
            )
            return
        self.settings.flush()
        caption_sync_mode, caption_offset_ms = self.settings.caption_timing()
        caption_y_ratio, caption_avoid_faces, _, _ = self.settings.caption_positioning()
        options = ProjectOptions(
            source=source, clip_count=self.clip_count.value(), min_duration=self.min_duration.value(),
            max_duration=self.max_duration.value(), aspect_ratio=self.aspect.currentText(),
            resolution=self.resolution.currentText(), face_tracking=(reframe_mode != "off"),
            reframe_mode=reframe_mode,
            reframe_style=reframe_style,
            captions=self.captions.isChecked(), caption_template=self.template_combo.currentText(),
            caption_sync_mode=caption_sync_mode, caption_offset_ms=caption_offset_ms,
            caption_y_ratio=caption_y_ratio, caption_avoid_faces=caption_avoid_faces,
            provider=provider_name, model=resolved_cfg["model"],
            multimodal=self.multimodal_check.isChecked(),
            cache_enabled=self.cache.isChecked(), whisper_model=self.whisper_model.currentText(),
            whisper_device=self.whisper_device.currentText(), language=self.settings.speech().language or None,
            output_dir=self.settings.data.get("output_dir", ""),
            project_name=self.project_name.text().strip() or default_name,
            youtube_metadata=asdict(metadata) if metadata else None,
            srt_path=srt_path,
            srt_offset_seconds=srt_offset,
            highlight_mode=str(self.highlight_mode_combo.currentData() or "ai"),
            ai_weight=round(self.hybrid_weight_spin.value() / 100.0, 2),
            use_youtube_subtitles=(not using_local and self.use_yt_subtitles_check.isChecked()),
            youtube_subtitle_language=self.settings.speech().language or "",
        )
        project_id = self.database.create_project(source, asdict(options))
        self.template_combo.setCurrentText(self.default_template())
        if self.worker_thread or self.queue_paused:
            reason = "queue paused" if self.queue_paused else "added to queue"
            self._log("INFO", f"Queued project {options.project_name or project_id} ({reason}).")
            self.refresh_all()
            self.show_page(2)
            return
        self._start_worker(project_id, options)

    def _start_worker(
        self,
        project_id: str,
        options: ProjectOptions,
        *,
        resume_analysis: bool = False,
        explicit_retry: bool = False,
    ) -> None:
        if self.worker_thread is not None:
            self._log("WARNING", f"Skipped duplicate start for {project_id}: another job is already running.")
            return
        vid = youtube_id(options.source) if is_youtube_url(options.source) else ""
        if download_coordinator().is_download_active(project_id=project_id, video_id=vid or ""):
            self._log("WARNING", f"Download for {project_id} is already in progress.")
            return
        self.pipeline.manager.reset()
        self._active_project_id = project_id
        self.worker_thread = QThread(self)
        self.worker = PipelineWorker(
            self.pipeline, project_id, options, resume_analysis=resume_analysis, explicit_retry=explicit_retry,
        )
        self.worker.moveToThread(self.worker_thread)
        self.worker_thread.started.connect(self.worker.run)
        self.worker.progress.connect(self.on_progress)
        self.worker.download_progress.connect(self.on_download_progress)
        self.worker.download_log.connect(self.on_download_log)
        self.worker.speech_progress.connect(self.on_speech_progress)
        self.worker.speech_log.connect(self.on_download_log)
        self.worker.warning.connect(self._provider_warning)
        self.worker.completed.connect(self.on_completed)
        self.worker.paused.connect(self.on_paused)
        self.worker.failed.connect(self.on_failed)
        self.worker.finished.connect(self.worker_thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.worker_thread.finished.connect(self._worker_finished)
        self.cancel_button.setEnabled(True)
        self.retry_analysis_button.setEnabled(False)
        progress = 40 if resume_analysis else 0
        project_row = next((p for p in self.database.projects() if p["id"] == project_id), None)
        thumb_path = project_thumbnail_path(project_row, self.clip_rows or self.database.clips())
        self.active_job_card.start_job(
            project_row,
            options_dict=asdict(options),
            profile_name=self.settings.performance().profile,
            initial_progress=progress,
            status_message="Retrying AI analysis" if resume_analysis else "Starting pipeline",
            thumb_path=thumb_path,
        )
        self.show_speech(False)
        self.show_download(not resume_analysis and is_youtube_url(options.source))
        self.worker_thread.start(); self.show_page(2)
        self.refresh_all()

    def _selected_project(self) -> dict | None:
        return self.queue_panel.selected_project()

    def _update_retry_button(self) -> None:
        project = self._selected_project()
        self.retry_analysis_button.setEnabled(
            bool(project and project["status"] == "analysis_failed" and not self.worker_thread
                 and not self.speech_utility_active and not self.closing)
        )

    def retry_analysis(self) -> None:
        project = self._selected_project()
        if not project or self.worker_thread or self.speech_utility_active or project["status"] != "analysis_failed":
            return
        try:
            options = ProjectOptions(**json.loads(project["options"]))
        except (ValueError, TypeError):
            QMessageBox.warning(self, "Cannot retry AI analysis", "This project's saved settings could not be read.")
            return
        options.provider = self.settings.data["active_provider"]
        resolved_cfg = self.settings.resolve_provider_config(options.provider)
        if resolved_cfg["api_key"] and not resolved_cfg["model"]:
            QMessageBox.warning(
                self, "AI model required",
                f"No model ID is selected for the '{options.provider}' provider.\n\n"
                "Open AI Provider Settings, select or type a model ID, and save before retrying.",
            )
            return
        options.model = resolved_cfg["model"]
        options.multimodal = options.multimodal and options.provider == "gemini"
        self._start_worker(project["id"], options, resume_analysis=True)

    def toggle_queue_paused(self, paused: bool) -> None:
        self.queue_paused = bool(paused)
        self.queue_overview.set_paused(self.queue_paused)
        self._log("INFO", "Queue paused by user." if self.queue_paused else "Queue resumed by user.")
        self._refresh_queue_overview()
        if not self.queue_paused and not self.worker_thread and not self.speech_utility_active and not self.closing:
            self.dispatch_next_queued_job()

    def dispatch_next_queued_job(self) -> bool:
        if self.queue_paused or self.worker_thread or self.speech_utility_active or self.closing:
            return False
        queued = self.database.next_queued_project()
        if not queued:
            return False
        try:
            options = ProjectOptions(**json.loads(queued["options"]))
        except (ValueError, TypeError):
            self.database.update_project(queued["id"], status="failed", error="Saved project options could not be read.")
            self.refresh_all()
            return False
        self._start_worker(queued["id"], options)
        return True

    def _project_output_folder(self, project: dict) -> Path:
        try:
            options = json.loads(project.get("options") or "{}")
        except (TypeError, ValueError):
            options = {}
        custom_root = str(options.get("output_dir") or "").strip() if isinstance(options, dict) else ""
        root = Path(custom_root) if custom_root else OUTPUT_DIR
        folder = root / str(project["id"])
        if folder.exists():
            return folder
        source = str(project.get("source") or "").strip()
        if source and not is_youtube_url(source):
            src_parent = Path(source).parent
            if src_parent.exists():
                return src_parent
        return root if root.exists() else OUTPUT_DIR

    @Slot(str, str)
    def _handle_queue_action(self, action: str, project_id: str) -> None:
        projects = self.project_rows or self.database.projects()
        project = next((p for p in projects if str(p["id"]) == project_id), None)
        if not project:
            return
        if action == "view_details":
            clips = [c for c in (self.clip_rows or self.database.clips()) if str(c.get("project_id") or "") == project_id]
            JobDetailsDialog(project, len(clips), self).exec()
        elif action == "open_folder":
            folder = self._project_output_folder(project)
            if folder.exists():
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
        elif action == "view_clips":
            clips = [c for c in (self.clip_rows or self.database.clips()) if str(c.get("project_id") or "") == project_id]
            self.show_page(3)
            if hasattr(self, "clips_library"):
                self.clips_library.filter_by_project(project_id, project_display_name(project))
                if clips:
                    self.clips_library.select_clip(str(clips[0]["id"]))
        elif action == "pause_active":
            self.pause_processing()
        elif action == "cancel_active":
            self.cancel_processing()
        elif action == "move_up":
            if self.database.reorder_queued_project(project_id, -1):
                self.refresh_all()
        elif action == "move_down":
            if self.database.reorder_queued_project(project_id, 1):
                self.refresh_all()
        elif action == "cancel_queued":
            if project.get("status") == "queued":
                self.database.update_project(project_id, status="cancelled", error="Cancelled by user", stage="Cancelled")
                self._log("WARNING", f"Cancelled queued job {project_id}.")
                self.refresh_all()
        elif action == "cancel_paused":
            dl_state = parse_download_state(project)
            if dl_state and dl_state.workspace_dir:
                cleanup_workspace(dl_state.workspace_dir)
            shutil.rmtree(CACHE_DIR / "jobs" / project_id, ignore_errors=True)
            self.database.update_project(
                project_id, status="cancelled", error="Cancelled by user", stage="Cancelled", download_state="",
            )
            self._log("WARNING", f"Cancelled job {project_id} and cleaned partial files.")
            self.refresh_all()
        elif action == "retry_analysis":
            self.queue_panel.select_project(project_id)
            self.retry_analysis()
        elif action in ("resume_download", "retry_auth", "retry_failed"):
            try:
                options = ProjectOptions(**json.loads(project["options"]))
            except (ValueError, TypeError):
                QMessageBox.warning(self, "Cannot retry job", "This project's saved settings could not be read.")
                return
            vid = youtube_id(options.source) if is_youtube_url(options.source) else ""
            coordinator = download_coordinator()
            if coordinator.is_download_active(project_id=project_id, video_id=vid or ""):
                self._log("WARNING", f"Job {project_id} is already actively downloading.")
                return
            dl_state = parse_download_state(project)
            rem_state = dl_state.remaining_cooldown() if dl_state else 0.0
            rem_coord, _ = coordinator.cooldown_remaining(vid or "")
            remaining = max(rem_state, rem_coord)
            if remaining > 0.5:
                self._log("WARNING", f"Rate limit cooldown active: retry available in {format_cooldown(remaining)}.")
                return
            explicit = action == "retry_auth"
            if explicit:
                coordinator.clear_auth_suspension(vid or "")
            if self.worker_thread or self.queue_paused or self.speech_utility_active:
                self.database.update_project(project_id, status="queued", error="", stage="Queued")
                self._log("INFO", f"Queued {project_id} to resume when the current task finishes.")
                self.refresh_all()
                return
            self.database.update_project(project_id, status="queued", error="", stage="Resuming")
            self.refresh_all()
            self._start_worker(project_id, options, explicit_retry=explicit)
        elif action == "remove_history":
            if project.get("status") == "processing":
                return
            dl_state = parse_download_state(project)
            if dl_state and dl_state.workspace_dir:
                cleanup_workspace(dl_state.workspace_dir)
            if self.database.delete_project_history(project_id):
                self.refresh_all()

    def _log(self, level: str, message: str) -> None:
        self.live_logs.add_entry(level, message)
        self.logs_panel.refresh_count()

    def _refresh_queue_overview(self, stage_message: str = "") -> None:
        projects = self.project_rows or self.database.projects()
        msg_lower = stage_message.lower()
        if self.worker_thread:
            engine_state = (
                "Waiting"
                if any(tok in msg_lower for tok in ("waiting", "gaming mode", "thermal", "cooling", "throttl"))
                else "Processing"
            )
        elif self.queue_paused:
            engine_state = "Paused"
        else:
            engine_state = "Idle"
        self.queue_overview.update_summary(
            projects,
            engine_state=engine_state,
            profile_name=self.settings.performance().profile,
            paused=self.queue_paused,
        )

    def pause_processing(self) -> None:
        downloading = not self.download_view.isHidden()
        if self.worker and downloading and self.pause_button.isEnabled():
            self.pause_button.setEnabled(False)
            self.cancel_button.setEnabled(False)
            self.download_view.cancel_button.setEnabled(False)
            self.download_view.status.setText("Pausing download (preserving partial files)…")
            self._log("INFO", "Pausing active download and preserving partial files…")
            self.pipeline.manager.pause()

    def cancel_processing(self) -> None:
        downloading = not self.download_view.isHidden()
        transcribing = not self.speech_progress.isHidden()
        if self.worker and (self.cancel_button.isEnabled() or self.download_view.cancel_button.isEnabled() or self.speech_progress.cancel_button.isEnabled()):
            self.pause_button.setEnabled(False)
            self.cancel_button.setEnabled(False)
            self.download_view.cancel_button.setEnabled(False)
            self.speech_progress.cancel_button.setEnabled(False)
            if downloading:
                self.download_view.status.setText("Cancelling download…")
            elif transcribing:
                self.speech_progress.stage.setText("Cancelling transcription…")
            else:
                self.queue_status.setText("Cancelling…")
            self.pipeline.manager.cancel()

    def show_download(self, active: bool) -> None:
        if active:
            self.active_job_card.ensure_active_visible()
            self.download_view.reset()
        else:
            self.download_view.animation.stop()
        self.download_view.setVisible(active)
        self._update_processing_rows()

    def show_speech(self, active: bool) -> None:
        if active:
            self.active_job_card.ensure_active_visible()
            self.speech_progress.begin()
        else:
            self.speech_progress.timer.stop()
        self.speech_progress.setVisible(active)
        self._update_processing_rows()

    def _update_processing_rows(self) -> None:
        downloading = not self.download_view.isHidden()
        transcribing = not self.speech_progress.isHidden()
        visible = (not downloading) and (not transcribing)
        self.processing_status_row.setVisible(visible)
        self.processing_progress_row.setVisible(visible)
        mode = "download" if downloading else ("speech" if transcribing else "generic")
        self.active_job_card.set_stage_mode(mode)

    @Slot(object)
    def on_download_progress(self, progress: DownloadProgress) -> None:
        if (
            not self.download_view.isHidden()
            and not self.pipeline.manager.cancel_event.is_set()
            and not self.pipeline.manager.pause_event.is_set()
        ):
            self.download_view.update_transfer(progress)
            reliable = bool(progress.total and progress.downloaded is not None and progress.eta_seconds is not None)
            self.active_job_card.set_stage_telemetry(eta_seconds=progress.eta_seconds, reliable=reliable)

    @Slot(str)
    def on_download_log(self, message: str) -> None:
        self._log("INFO", message)

    @Slot(object)
    def on_speech_progress(self, progress: SpeechProgress) -> None:
        if (
            not self.speech_progress.isHidden()
            and not self.pipeline.manager.cancel_event.is_set()
            and not self.pipeline.manager.pause_event.is_set()
        ):
            self.speech_progress.update_transcription(progress)
            self.active_job_card.set_stage_telemetry(
                eta_seconds=progress.eta,
                reliable=bool(progress.reliable_percentage and progress.processed is not None and progress.duration),
            )

    def on_progress(self, value: int, message: str) -> None:
        if self.pipeline.manager.cancel_event.is_set() or self.pipeline.manager.pause_event.is_set():
            return
        if value >= 12 and not self.download_view.isHidden():
            self.show_download(False)
        if value == 12:
            self.show_speech(True)
        elif value >= 40 and not self.speech_progress.isHidden():
            self.show_speech(False)
        self.active_job_card.update_stage(value, message)
        self._log("INFO", message)
        self.metric_active.findChild(QLabel, "metricValue").setText(f"{value}%")
        self._refresh_queue_overview(message)

    def on_completed(self, project_id: str, count: int) -> None:
        elapsed_s = self.active_job_card.elapsed_seconds()
        self.show_download(False)
        self.show_speech(False)
        self.active_job_card.mark_finished("completed", f"Completed {count} clips")
        self._log("SUCCESS", "All outputs passed MP4 validation.")
        self.refresh_all()
        self._show_job_completed_popup(project_id, count, elapsed_s)

    def _show_job_completed_popup(self, project_id: str, count: int, elapsed_seconds: float | None) -> None:
        if self.closing or count <= 0 or not project_id or project_id in self._completed_popups_shown:
            return
        self._completed_popups_shown.add(project_id)
        projects = self.project_rows or self.database.projects()
        project = next((p for p in projects if str(p["id"]) == str(project_id)), None)
        clips = [c for c in (self.clip_rows or self.database.clips()) if str(c.get("project_id") or "") == str(project_id)]
        combined_duration = sum(float(c.get("duration") or 0.0) for c in clips)
        title = project_display_name(project) if project else "Completed Project"

        if self._active_completion_dialog is not None:
            try:
                self._active_completion_dialog.close()
            except RuntimeError:
                pass
            self._active_completion_dialog = None

        dlg = JobCompletedDialog(
            str(project_id), title, len(clips) or count, combined_duration, elapsed_seconds, self,
        )
        dlg.view_clips_requested.connect(self._navigate_to_project_clips)
        dlg.destroyed.connect(lambda: setattr(self, "_active_completion_dialog", None))
        self._active_completion_dialog = dlg
        dlg.show()
        dlg.raise_()

    def _navigate_to_project_clips(self, project_id: str) -> None:
        projects = self.project_rows or self.database.projects()
        project = next((p for p in projects if str(p["id"]) == str(project_id)), None)
        clips = [c for c in (self.clip_rows or self.database.clips()) if str(c.get("project_id") or "") == str(project_id)]
        self.show_page(3)
        if hasattr(self, "clips_library"):
            self.clips_library.filter_by_project(str(project_id), project_display_name(project) if project else "")
            if clips:
                self.clips_library.select_clip(str(clips[0]["id"]))

    def on_paused(self, project_id: str, message: str) -> None:
        self.show_download(False)
        self.show_speech(False)
        self.refresh_all()
        self.active_job_card.mark_finished("paused", message)
        self._log("INFO", f"{message} (job {project_id}). Click Resume Download to continue.")
        self.metric_active.findChild(QLabel, "metricValue").setText("Paused")

    def on_failed(self, project_id: str, message: str) -> None:
        self.show_download(False)
        self.show_speech(False)
        self.refresh_all()
        cancelled = message == "Processing cancelled"
        project_status = "failed"
        for project in self.project_rows or []:
            if project["id"] == project_id:
                project_status = str(project.get("status") or "failed")
                if project_status in ("analysis_failed", "rate_limited", "auth_required"):
                    self.queue_panel.select_project(project_id)
                break
        retryable = project_status == "analysis_failed"
        rate_limited = project_status == "rate_limited"
        auth_required = project_status == "auth_required"
        summary = message.partition("\n")[0]
        display_msg = f"{summary}\nSelect Retry AI Analysis to continue." if retryable else summary
        card_status = (
            "cancelled" if cancelled
            else ("analysis_failed" if retryable else (project_status if (rate_limited or auth_required) else "failed"))
        )
        self.active_job_card.mark_finished(card_status, display_msg)
        self._log("WARNING" if (cancelled or rate_limited or auth_required) else "ERROR", message)
        state = (
            "Cancelled" if cancelled
            else ("Rate Limited" if rate_limited else ("Auth Required" if auth_required else "Error"))
        )
        self.metric_active.findChild(QLabel, "metricValue").setText(state)
        if not cancelled and not retryable and not self.closing:
            low = message.lower()
            needs_deps = (not rate_limited and not auth_required) and any(
                tok in low for tok in ("yt-dlp", "ffmpeg", "ffprobe", "dependency manager", "javascript runtime", "403")
            )
            # Never offer immediate retry inside modal for active HTTP 429 cooldown or auth verification!
            can_retry = (not rate_limited and not auth_required) and any(
                tok in low for tok in ("retry", "403", "network", "timed out", "format", "merging")
            )
            title = (
                "YouTube Rate Limit Active" if rate_limited
                else ("YouTube Verification Required" if auth_required else "Processing failed")
            )
            dlg = ErrorDetailsDialog(
                title,
                message,
                allow_retry=can_retry,
                allow_dependencies=needs_deps,
                parent=self,
            )
            dlg.exec()
            if dlg.chosen_action == "dependencies":
                self.show_route("settings.dependencies")
            elif dlg.chosen_action == "retry":
                self._handle_queue_action("resume_download", project_id)

    def _worker_finished(self) -> None:
        self.pause_button.setEnabled(False)
        self.cancel_button.setEnabled(False)
        if self.worker_thread:
            self.worker_thread.deleteLater()
        self.worker_thread = None; self.worker = None; self._active_project_id = None
        self.refresh_all()
        started_next = self.dispatch_next_queued_job()
        if not started_next:
            self.active_job_card.show_idle()
            self.metric_active.findChild(QLabel, "metricValue").setText("Paused" if self.queue_paused else "Idle")
        self._update_retry_button()
        self._refresh_queue_overview()
        self._finish_close()

    def refresh_all(self) -> None:
        projects, clips = self.database.projects(), self.database.clips()
        projects_changed = projects != self.project_rows
        clips_changed = clips != self.clip_rows
        if projects_changed or clips_changed:
            selected_project = self._selected_project()
            self._fill_projects(self.recent_table, projects[:8], compact=True)
            with QSignalBlocker(self.queue_panel):
                self.queue_panel.set_data(projects, clips)
                if selected_project:
                    self.queue_panel.select_project(str(selected_project["id"]))
            self.metric_projects.findChild(QLabel, "metricValue").setText(str(self.database.project_count()))
            self.project_rows = projects
            self._update_retry_button()
            self._refresh_queue_overview()
        if not clips_changed:
            return
        busy_ids: set[str] = set()
        for clip in clips:
            thumb_file = Path(clip.get("thumbnail_path") or "")
            clip_file = Path(clip["path"])
            if clip_file.is_file() and not thumb_file.is_file():
                self.thumb_runner.submit(
                    clip["id"], clip_file, title=clip["title"],
                    profile=self.settings.performance().profile,
                    existing_cache_key=clip.get("thumbnail_cache_key", ""),
                )
            if self.thumb_runner.is_busy(clip["id"]):
                busy_ids.add(str(clip["id"]))
        on_clips_page = hasattr(self, "stack") and self.stack.currentIndex() == 3
        with QSignalBlocker(self.clips_library):
            self.clips_library.set_clips(clips, busy_ids=busy_ids)
        self.metric_clips.findChild(QLabel, "metricValue").setText(str(self.database.clip_count()))
        self.clip_rows = clips
        if on_clips_page:
            self.preview_selected()
        elif hasattr(self, "clip_detail") and not self.clip_detail.player.source().isEmpty():
            self.clip_detail.release_media()

    def _fill_projects(self, table: QTableWidget, projects: list[dict], compact: bool) -> None:
        table.setRowCount(len(projects))
        for row, project in enumerate(projects):
            source = Path(project["source"]).name if not is_youtube_url(project["source"]) else project["source"]
            try:
                options = json.loads(project["options"])
            except (TypeError, ValueError):
                options = {}
            name = (options.get("project_name") if isinstance(options, dict) else None) or source
            status = "AI analysis failed" if project["status"] == "analysis_failed" else project["status"]
            detail = (project["created_at"][:16].replace("T", " ") if compact else redact(project["error"]))
            values = [redact(str(name)), status, f"{project['progress']}%", detail.partition("\n")[0]]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setToolTip(redact(source))
                if column == 0 and not compact:
                    item.setData(Qt.ItemDataRole.UserRole, project)
                table.setItem(row, column, item)

    @Slot(dict)
    def _on_clip_selected(self, clip: dict) -> None:
        cid = str(clip.get("id") or "")
        self.clip_detail.display_clip(clip or None, busy=bool(cid and self.thumb_runner.is_busy(cid)))

    def preview_selected(self) -> None:
        clip = self._selected_clip()
        cid = str(clip.get("id") or "") if clip else ""
        self.clip_detail.display_clip(clip, busy=bool(cid and self.thumb_runner.is_busy(cid)))

    @Slot(str, object)
    def _on_thumbnail_ready(self, clip_id: str, result) -> None:
        self.database.update_clip_thumbnail(
            clip_id, str(result.preview_path), str(result.raw_path), result.timestamp, result.cache_key,
        )
        self.clips_library.set_clip_busy(clip_id, False)
        self.clip_rows = None
        self.refresh_all()

    @Slot(str, str)
    def _on_thumbnail_failed(self, clip_id: str, message: str) -> None:
        LOG.warning("Thumbnail generation failed for %s: %s", clip_id, message)
        self.clips_library.set_clip_busy(clip_id, False)
        selected = self._selected_clip()
        if selected and selected["id"] == clip_id:
            self.clip_detail.stage.thumb_preview.set_thumbnail(None, float(selected["duration"]), status="Thumbnail unavailable")

    def regenerate_thumbnail(self) -> None:
        clip = self._selected_clip()
        if not clip:
            return
        clip_path = Path(clip["path"])
        if not clip_path.is_file():
            return
        self.clips_library.set_clip_busy(clip["id"], True)
        self.clip_detail.display_clip(clip, busy=True)
        self.thumb_runner.submit(
            clip["id"], clip_path, title=clip["title"],
            profile=self.settings.performance().profile, force=True,
        )

    def select_thumbnail_frame(self) -> None:
        clip = self._selected_clip()
        if not clip:
            return
        clip_path = Path(clip["path"])
        if not clip_path.is_file():
            return
        dialog = FramePickerDialog(clip_path, float(clip["duration"]), clip.get("thumbnail_timestamp"), self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.clips_library.set_clip_busy(clip["id"], True)
        self.clip_detail.display_clip(clip, busy=True)
        self.thumb_runner.submit(
            clip["id"], clip_path, title=clip["title"],
            timestamp=dialog.selected_timestamp,
            profile=self.settings.performance().profile, force=True,
        )

    def export_selected_thumbnail(self) -> None:
        clip = self._selected_clip()
        if not clip:
            return
        raw_path = Path(clip.get("thumbnail_raw_path") or "")
        source = raw_path if raw_path.is_file() else Path(clip["path"])
        if not source.is_file():
            QMessageBox.warning(self, "Export unavailable", "Neither the clip nor its raw thumbnail frame was found.")
            return
        options_dialog = ExportThumbnailDialog(clip["title"], self)
        if options_dialog.exec() != QDialog.DialogCode.Accepted:
            return
        default_name = f"{Path(clip['path']).stem}-thumbnail-1080x1920.jpg"
        filename, _ = QFileDialog.getSaveFileName(
            self, "Export Thumbnail", default_name, "JPEG images (*.jpg);;PNG images (*.png)",
        )
        if not filename:
            return
        try:
            export_thumbnail(
                source, Path(filename), title=clip["title"],
                overlay_title=options_dialog.overlay_check.isChecked(),
                size=(1080, 1920), template_name=self.default_template(),
                timestamp=clip.get("thumbnail_timestamp"),
            )
        except Exception as error:
            QMessageBox.warning(self, "Thumbnail export failed", redact(str(error)))

    def rename_selected_clip(self) -> None:
        clip = self._selected_clip()
        if not clip:
            return
        new_title, ok = QInputDialog.getText(self, "Rename Clip", "Clip title:", text=str(clip["title"]))
        if not ok or not new_title.strip():
            return
        if self.database.rename_clip(clip["id"], new_title):
            self.clip_rows = None
            self.refresh_all()

    def open_selected_clip_folder(self) -> None:
        clip = self._selected_clip()
        if clip:
            self._open_clip_folder_item(clip)

    def _open_clip_folder_item(self, clip: dict) -> None:
        path = Path(str(clip.get("path") or ""))
        if not reveal_in_file_manager(path):
            QMessageBox.information(self, "Folder unavailable", "The clip folder could not be found on disk.")
        if hasattr(self, "clips_library"):
            self.clips_library.refresh_file_availability()
            self.preview_selected()

    def export_all_clips(self) -> None:
        clips = self.clip_rows or self.database.clips()
        valid_files = [Path(c["path"]) for c in clips if Path(c["path"]).is_file()]
        if not valid_files:
            QMessageBox.information(self, "No clips to export", "No valid exported clip files were found.")
            return
        dest = QFileDialog.getExistingDirectory(self, "Choose Export Folder", str(OUTPUT_DIR))
        if not dest:
            return
        dest_dir = Path(dest)
        copied = 0
        for src in valid_files:
            target = dest_dir / src.name
            if src.resolve() != target.resolve():
                shutil.copy2(src, target)
            copied += 1
        QMessageBox.information(self, "Export complete", f"Exported {copied} clip(s) to:\n{dest_dir}")

    def delete_selected_clip(self) -> None:
        clip = self._selected_clip()
        if clip:
            self._confirm_and_delete_clips([clip])

    def _confirm_and_delete_clips(self, clips: list[dict]) -> None:
        if not clips:
            return
        dialog = ClipDeleteDialog(clips, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.execute_clip_deletion(clips, delete_files=dialog.delete_files)

    def execute_clip_deletion(self, clips: list[dict], *, delete_files: bool = False) -> None:
        def _release_handles(target_paths: set[Path]) -> None:
            active_src = self.player.source().toLocalFile()
            current_clip = self._selected_clip()
            target_ids = {str(c.get("id") or "") for c in clips}
            matches_current = bool(current_clip and str(current_clip.get("id") or "") in target_ids)
            matches_src = False
            if active_src:
                try:
                    matches_src = Path(active_src).resolve() in target_paths
                except OSError:
                    matches_src = False
            if matches_current or matches_src:
                self.clip_detail.release_media()
                app = QApplication.instance()
                if app is not None:
                    app.processEvents()

        report = delete_clips_transaction(
            clips,
            self.database,
            delete_files=delete_files,
            release_handles=_release_handles,
        )
        deleted_set = set(report.deleted_ids)
        if deleted_set and hasattr(self, "clips_library"):
            self.clips_library.sync_after_deletion(deleted_set)
        self.clip_rows = None
        self.refresh_all()
        if report.failed_items:
            summary_lines = [f"• {title}: {err}" for title, err in report.failed_items[:6]]
            if len(report.failed_items) > 6:
                summary_lines.append(f"• …and {len(report.failed_items) - 6} more.")
            QMessageBox.warning(
                self,
                "Some clip files could not be removed",
                "Removed "
                f"{len(report.deleted_ids)} of {report.requested_count} clip(s).\n\n"
                "The remaining items were kept in your library because their files could not be moved to Trash / Recycle Bin:\n"
                + "\n".join(summary_lines),
            )

    def toggle_play(self) -> None:
        self.clip_detail.toggle_play()

    def _selected_clip(self) -> dict | None:
        return self.clips_library.selected_clip()

    def open_selected_clip(self) -> None:
        clip = self._selected_clip()
        if clip:
            self._open_clip_item(clip)

    def _open_clip_item(self, clip: dict) -> None:
        path = Path(str(clip.get("path") or ""))
        if not open_clip_file(path):
            QMessageBox.warning(self, "Clip file missing", f"Could not open '{path.name}' because the file no longer exists on disk.")
        if hasattr(self, "clips_library"):
            self.clips_library.refresh_file_availability()
            self.preview_selected()

    def open_output_folder(self) -> None:
        path = Path(self.settings.data.get("output_dir") or OUTPUT_DIR)
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def default_template(self) -> str:
        name = self.settings.data.get("caption_template")
        return name if isinstance(name, str) and name in TEMPLATES else TEMPLATE_DEFAULT

    def choose_template(self, name: str) -> None:
        previous = self.default_template()
        self.settings.data["caption_template"] = name
        try:
            self.settings.save()
        except OSError as error:
            self.settings.data["caption_template"] = previous
            log_error("Could not save caption template", error)
            self.caption_library.show_notice("Could not save the default caption template. Try again.")
            return
        self.caption_library.set_selected(name)
        self.caption_library.show_notice("")
        if self.template_combo.currentText() == previous:
            self.template_combo.setCurrentText(name)

    def _on_caption_timing_changed(self, mode: str, offset_ms: int) -> None:
        if self._hydrating_ui:
            return
        try:
            self.settings.update_caption_timing(mode, offset_ms, debounced=False)
        except OSError as error:
            log_error("Could not save caption timing preferences", error)
            self.caption_library.show_notice("Could not save caption timing settings. Try again.")
            return
        self.caption_library.show_notice("")

    def _on_caption_positioning_changed(self, y_ratio: float, avoid_faces: bool, show_safe_area: bool, platform: str) -> None:
        if self._hydrating_ui:
            return
        try:
            self.settings.update_caption_positioning(
                y_ratio=y_ratio,
                avoid_faces=avoid_faces,
                show_safe_area=show_safe_area,
                platform=platform,
                debounced=False,
            )
        except OSError as error:
            log_error("Could not save caption positioning preferences", error)
            self.caption_library.show_notice("Could not save caption positioning settings. Try again.")
            return
        self.caption_library.show_notice("")

    def _on_reframe_mode_changed(self) -> None:
        mode = str(self.reframe_combo.currentData() or "auto")
        self.face_tracking.setChecked(mode != "off")
        if hasattr(self, "reframe_style_combo"):
            self.reframe_style_combo.setEnabled(mode not in ("off", "fit_original"))
        self._persist_project_preferences()

    def _on_project_template_changed(self, name: str) -> None:
        if self._hydrating_ui or not name or name not in TEMPLATES:
            return
        self.settings.data["caption_template"] = name
        self.settings.save_debounced()

    def _on_project_whisper_changed(self) -> None:
        if self._hydrating_ui:
            return
        self.settings.data["whisper_model"] = self.whisper_model.currentText()
        self.settings.data["whisper_device"] = self.whisper_device.currentText()
        self.settings.save_debounced()

    def _on_project_model_edited(self) -> None:
        if self._hydrating_ui:
            return
        model = self.project_model.text().strip()
        if not model:
            return
        provider = self.project_provider.currentText()
        self.settings.set_provider_model(provider, model, persist=False)
        self.settings.save_debounced()
        if hasattr(self, "settings_provider") and self.settings_provider.currentText() == provider:
            with QSignalBlocker(self.model_input):
                if self.model_input.findText(model) < 0:
                    self.model_input.insertItem(0, model)
                self.model_input.setEditText(model)

    def _persist_project_preferences(self) -> None:
        if self._hydrating_ui:
            return
        if not hasattr(self, "clip_count") or not hasattr(self, "reframe_combo") or not hasattr(self, "highlight_mode_combo"):
            return
        self.settings.update_project_defaults({
            "clip_count": int(self.clip_count.value()),
            "min_duration": int(self.min_duration.value()),
            "max_duration": int(self.max_duration.value()),
            "aspect_ratio": self.aspect.currentText(),
            "resolution": self.resolution.currentText(),
            "reframe_mode": str(self.reframe_combo.currentData() or "auto"),
            "reframe_style": str(self.reframe_style_combo.currentData() or "natural") if hasattr(self, "reframe_style_combo") else "natural",
            "captions": bool(self.captions.isChecked()),
            "cache_enabled": bool(self.cache.isChecked()),
            "highlight_mode": str(self.highlight_mode_combo.currentData() or "ai"),
            "ai_weight_pct": int(self.hybrid_weight_spin.value()),
            "multimodal": bool(self.multimodal_check.isChecked()),
            "use_youtube_subtitles": bool(self.use_yt_subtitles_check.isChecked()),
        }, debounced=True)

    def _project_provider_changed(self, name: str) -> None:
        with QSignalBlocker(self.project_model):
            self.project_model.setText(self.settings.provider(name).get("model", ""))
        self.multimodal_check.setEnabled(name == "gemini")
        if name != "gemini":
            with QSignalBlocker(self.multimodal_check):
                self.multimodal_check.setChecked(False)
        if not self._hydrating_ui:
            self.settings.data["active_provider"] = name
            self.settings.save_debounced()

    def _aspect_changed(self, ratio: str) -> None:
        recommended = {"9:16": "1080x1920", "1:1": "1080x1080", "4:5": "1080x1350", "16:9": "1920x1080"}
        self.resolution.setCurrentText(recommended.get(ratio, "1080x1920"))
        self._persist_project_preferences()

    def _populate_model_combo(self, saved_model: str, cached_models: list[str]) -> None:
        """Populate model_input cleanly without firing spurious signals or losing manual/saved model IDs."""
        items: list[str] = []
        cleaned_saved = saved_model.strip()
        if cleaned_saved:
            items.append(cleaned_saved)
        for item in cached_models:
            text = str(item).strip()
            if text and text not in items:
                items.append(text)
        with QSignalBlocker(self.model_input):
            self.model_input.clear()
            if items:
                self.model_input.addItems(items)
            self.model_input.setEditText(cleaned_saved)

    def load_provider_form(self, name: str) -> None:
        self._loading_provider_form = True
        try:
            values = self.settings.provider(name)
            with QSignalBlocker(self.provider_name_input):
                self.provider_name_input.setText(str(values.get("display_name") or name))
                self.provider_name_input.setReadOnly(name != "custom")
            with QSignalBlocker(self.api_key_input):
                self.api_key_input.setText(self.settings.api_key(name))
            with QSignalBlocker(self.base_url_input):
                self.base_url_input.setText(values.get("base_url", ""))
            saved_model = str(values.get("model") or "").strip()
            cached_models = list(values.get("cached_models") or [])
            self._populate_model_combo(saved_model, cached_models)
            with QSignalBlocker(self.fallback_models_input):
                self.fallback_models_input.setText(", ".join(values.get("fallback_models", [])))
            enabled = bool(values.get("auto_fallback"))
            with QSignalBlocker(self.auto_fallback_check):
                self.auto_fallback_check.setChecked(enabled)
            self.fallback_models_input.setEnabled(enabled)
        finally:
            self._loading_provider_form = False

    def _on_settings_model_committed(self) -> None:
        if self._hydrating_ui or self._loading_provider_form:
            return
        name = self.settings_provider.currentText()
        model = self.model_input.currentText().strip()
        if not model:
            return
        self.settings.set_provider_model(name, model, persist=True)
        if self.project_provider.currentText() == name:
            with QSignalBlocker(self.project_model):
                self.project_model.setText(model)

    def _fallback_models(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(
            model.strip() for model in self.fallback_models_input.text().split(",") if model.strip()
        ))

    def save_provider(self) -> None:
        name = self.settings_provider.currentText()
        model = self.model_input.currentText().strip()
        base_url = self.base_url_input.text().strip()
        display_name = self.provider_name_input.text().strip() if hasattr(self, "provider_name_input") else name
        self.settings.set_provider(
            name,
            base_url=base_url,
            model=model,
            display_name=display_name,
            fallback_models=list(self._fallback_models()),
            auto_fallback=self.auto_fallback_check.isChecked(),
            set_active=True,
            persist=False,
        )
        try:
            self.settings.set_api_key(name, self.api_key_input.text().strip())
            self.settings.save()
        except Exception as error:
            QMessageBox.critical(self, "Credential error", redact(str(error), self.api_key_input.text()))
            return
        with QSignalBlocker(self.project_provider):
            self.project_provider.setCurrentText(name)
        self._project_provider_changed(name)
        if model:
            self.connection_status.setText(f"Settings saved securely ({name} • {model}).")
        else:
            self.connection_status.setText("Connection saved securely. Load models or enter a model ID before running AI analysis.")

    def _form_provider(self) -> AIProvider:
        return AIProvider(
            self.settings_provider.currentText(), self.api_key_input.text().strip(),
            self.base_url_input.text().strip(), self.model_input.currentText().strip(),
            fallback_models=self._fallback_models(), auto_fallback=self.auto_fallback_check.isChecked(),
            cancel_event=self.utility_cancel,
        )

    def test_provider(self) -> None:
        provider = self._form_provider()
        if not provider.model.strip():
            self.connection_status.setText("Select or enter a model ID before testing the connection.")
            return
        self.connection_status.setText("Testing connection…")
        self._run_utility(provider, provider.test, lambda result: self.connection_status.setText(f"Connected: {result}"))

    def load_models(self) -> None:
        target_provider = self.settings_provider.currentText()
        typed_before = self.model_input.currentText().strip()
        self._model_load_seq += 1
        req_seq = self._model_load_seq
        provider = self._form_provider()
        self.connection_status.setText(f"Loading models for {target_provider}…")

        def done(models: object) -> None:
            # Ignore stale responses from an earlier request.
            if req_seq != self._model_load_seq:
                return
            values = [str(m).strip() for m in list(models) if str(m).strip()]  # type: ignore[arg-type]
            # Cache discovered model list separately without ever wiping the user's selected model.
            self.settings.set_cached_models(target_provider, values, persist=True)
            if self.settings_provider.currentText() != target_provider:
                self.connection_status.setText(f"Cached {len(values)} models for {target_provider}.")
                return

            saved_model = str(self.settings.provider(target_provider).get("model") or "").strip()
            live_text = self.model_input.currentText().strip()
            preserved = live_text or saved_model or typed_before
            if preserved:
                selected = preserved
            elif values:
                selected = values[0]
            else:
                selected = ""

            self._populate_model_combo(selected, values)
            if selected:
                self.settings.set_provider_model(target_provider, selected, persist=True)
                if self.project_provider.currentText() == target_provider:
                    with QSignalBlocker(self.project_model):
                        self.project_model.setText(selected)
                self.connection_status.setText(f"Loaded {len(values)} models (selected: {selected}).")
            else:
                self.connection_status.setText(f"Loaded {len(values)} models.")

        self._run_utility(provider, provider.list_models, done)

    def _speech_settings_page(self) -> QWidget:
        outer, layout = self._page("Transcription", "Configure local models, cloud engines, language preferences, and acceleration")
        self.speech_settings = SpeechSettingsView(self.settings, self._sync_speech_defaults, self._start_speech_task)
        layout.addWidget(self.speech_settings)
        layout.addStretch()
        return outer

    def _sync_speech_defaults(self) -> None:
        self.whisper_model.setCurrentText(self.settings.data["whisper_model"])
        self.whisper_device.setCurrentText(self.settings.data["whisper_device"])

    def _start_speech_task(self, function: Callable[[], object], done: Callable[[object], None]) -> bool:
        if self.worker_thread or self.utility_threads:
            self.speech_settings.status.setText("Wait for the current task to finish.")
            return False

        def action() -> object:
            try:
                return function()
            except Exception as error:
                raise ProviderError(redact(str(error))) from None

        self.speech_utility_active = True
        self.speech_settings.setEnabled(False)
        self.generate_button.setEnabled(False)
        self._update_retry_button()
        self._run_utility(None, action, done)
        return True

    def _run_utility(self, provider: AIProvider | None, function: Callable[[], object],
                     done: Callable[[object], None]) -> None:
        if self.utility_threads:
            return
        self.provider_group.setEnabled(False)
        self.utility_done = done
        self.utility_cancel.clear()
        thread = FunctionThread(function, self); self.utility_threads.append(thread)
        if provider:
            provider.notify = thread.warning.emit
        thread.succeeded.connect(self._utility_succeeded)
        thread.failed.connect(self._utility_failed)
        thread.warning.connect(self._utility_warning)
        thread.finished.connect(self._utility_finished)
        thread.start()

    @Slot(str)
    def _provider_warning(self, message: str) -> None:
        self.live_logs.add_entry("WARNING", message)
        if not self.pipeline.manager.cancel_event.is_set():
            self.queue_status.setText(message.rpartition("\n")[2])

    @Slot(str)
    def _utility_warning(self, message: str) -> None:
        self.live_logs.add_entry("WARNING", message)
        if not self.closing:
            self.connection_status.setText(message.rpartition("\n")[2])

    @Slot(object)
    def _utility_succeeded(self, result: object) -> None:
        if self.utility_done and not self.closing:
            self.utility_done(result)
            operation = "Speech-to-Text operation" if self.speech_utility_active else "AI provider operation"
            self.live_logs.add_entry("SUCCESS", f"{operation} completed.")
            LOG.log(SUCCESS, "%s completed", operation)

    @Slot(str)
    def _utility_failed(self, message: str) -> None:
        self.live_logs.add_entry("ERROR", message)
        operation = "Speech-to-Text operation" if self.speech_utility_active else "AI provider operation"
        LOG.error("%s failed\n%s", operation, message)
        if self.speech_utility_active:
            self.speech_settings.status.setText(message)
            return
        if not self.closing:
            summary = message.partition("\n")[0]
            self.connection_status.setText(f"{summary}\nSee Processing Queue for details.")

    def _utility_finished(self) -> None:
        thread = self.sender()
        self.utility_threads.remove(thread)
        thread.deleteLater()
        self.utility_done = None
        self.provider_group.setEnabled(True)
        if self.speech_utility_active:
            self.speech_utility_active = False
            self.speech_settings.setEnabled(True)
            self.generate_button.setEnabled(True)
            self._update_retry_button()
        self._finish_close()

    def _finish_close(self) -> None:
        if (self.closing and not self.worker_thread and not self.utility_threads
                and not self.caption_library.busy and not self.youtube_preview.busy
                and not self.local_preview.busy):
            self.close()

    def save_local_settings(self) -> None:
        self.settings.data["output_dir"] = self.output_input.text().strip()
        self.settings.data["hardware_encoding"] = self.hardware_check.isChecked()
        if hasattr(self, "general_cache_check"):
            cache_on = self.general_cache_check.isChecked()
            self.settings.data["cache_enabled"] = cache_on
            self.settings.update_project_defaults({"cache_enabled": cache_on}, debounced=False)
            if hasattr(self, "cache_check"):
                with QSignalBlocker(self.cache_check):
                    self.cache_check.setChecked(cache_on)
        self.settings.save()
        if hasattr(self, "general_status"):
            self.general_status.setText("General settings saved.")
        self.connection_status.setText("Local settings saved.")

    def reset_all_settings(self) -> None:
        answer = QMessageBox.question(
            self, "Reset application settings",
            "Reset all non-credential application preferences to their default values?\n\n"
            f"Saved API keys in {credential_store_name()} and existing projects/clips will be preserved.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.settings.reset_to_defaults(clear_credentials=False)
        self._hydrating_ui = True
        try:
            self.settings_provider.setCurrentText(self.settings.data["active_provider"])
            self.load_provider_form(self.settings_provider.currentText())
            self.project_provider.setCurrentText(self.settings.data["active_provider"])
            self.project_model.setText(self.settings.provider().get("model", ""))
            self.output_input.setText(self.settings.data.get("output_dir", ""))
            self.hardware_check.setChecked(bool(self.settings.data.get("hardware_encoding", True)))
            if hasattr(self, "general_cache_check"):
                self.general_cache_check.setChecked(bool(self.settings.data.get("cache_enabled", True)))
            if hasattr(self, "sidebar"):
                self.sidebar.set_collapsed(False, emit_signal=False)
                self.sidebar.set_settings_expanded(False, emit_signal=False)
            if hasattr(self, "sidebar_compact_check"):
                self.sidebar_compact_check.setChecked(False)
            appearance = str(self.settings.data.get("appearance") or "dark")
            for mode_id, btn in self.appearance_buttons.items():
                with QSignalBlocker(btn):
                    btn.setChecked(mode_id == appearance)
            self._theme.apply(appearance)
            if hasattr(self, "clip_detail"):
                self.clip_detail.set_audio_preferences(80, False)
            if hasattr(self, "caption_library"):
                self.caption_library.set_timing_preferences(*self.settings.caption_timing())
                self.caption_library.set_positioning_preferences(*self.settings.caption_positioning())
        finally:
            self._hydrating_ui = False
        if hasattr(self, "general_status"):
            self.general_status.setText("Settings reset to defaults.")
        self.connection_status.setText("Settings reset to defaults.")

    def closeEvent(self, event: QCloseEvent) -> None:
        if not self.closing:
            self.closing = True
            self.settings.update_ui_state({
                "window_width": int(self.width()),
                "window_height": int(self.height()),
                "window_maximized": bool(self.isMaximized()),
                "player_volume": int(self.clip_detail.volume_slider.value()) if hasattr(self, "clip_detail") else 80,
                "player_muted": bool(self.clip_detail.audio_output.isMuted()) if hasattr(self, "clip_detail") else False,
                "sidebar_collapsed": bool(self.sidebar.collapsed) if hasattr(self, "sidebar") else False,
                "settings_expanded": bool(self.sidebar.settings_expanded) if hasattr(self, "sidebar") else False,
                "last_settings_route": self.sidebar.last_settings_route if hasattr(self, "sidebar") else "settings.general",
            }, debounced=False)
            self.settings.flush()
            if hasattr(self, "clip_detail"):
                self.clip_detail.release_media()
            else:
                self.player.stop()
            self.caption_library.stop()
            if hasattr(self, "performance_view"):
                self.performance_view.set_polling(False)
            if hasattr(self, "thumb_runner"):
                self.thumb_runner.shutdown()
            self.youtube_preview.cancel()
            self.local_preview.cancel()
            self.pipeline.manager.cancel()
            self.utility_cancel.set()
        if self.worker_thread or self.utility_threads or self.caption_library.busy or self.youtube_preview.busy or self.local_preview.busy:
            self.setEnabled(False)
            self.setWindowTitle("AutoClip Studio — Shutting down…")
            event.ignore()
            return
        self.pipeline.monitor.shutdown()
        event.accept()


def run() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("AutoClip Studio")
    app.setApplicationDisplayName("AutoClip Studio")
    app.setOrganizationName("AutoClip")
    icon_path = asset_path("autoclip.icns" if sys.platform == "darwin" else "autoclip.ico")
    symbol_path = asset_path("symbol.png")
    launcher = APP_ROOT / "AutoClipStudio.exe"
    if icon_path.is_file():
        app.setWindowIcon(QIcon(str(icon_path)))
    elif symbol_path.is_file():
        app.setWindowIcon(QIcon(str(symbol_path)))
    elif launcher.is_file():
        app.setWindowIcon(QFileIconProvider().icon(QFileInfo(str(launcher))))
    window = MainWindow(); window.show()
    return app.exec()
