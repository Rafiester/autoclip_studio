from __future__ import annotations

import json
import shutil
import sys
from dataclasses import asdict
from pathlib import Path
from threading import Event
from typing import Callable

from PySide6.QtCore import QFileInfo, QSignalBlocker, QSize, QThread, Qt, QUrl, Slot
from PySide6.QtGui import QCloseEvent, QDesktopServices, QIcon, QImage, QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QFileIconProvider, QFormLayout, QFrame, QGridLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QProgressBar, QPushButton, QScrollArea, QSizePolicy, QSpinBox,
    QStackedWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .captions import TEMPLATE_DEFAULT, TEMPLATES
from .config import SettingsStore, credential_store_name
from .database import Database
from .diagnostics import LOG, SUCCESS, log_error, redact
from .media import DownloadProgress, is_youtube_url, youtube_id, youtube_source_error
from .models import ProjectOptions
from .paths import APP_ROOT, OUTPUT_DIR, asset_path, ensure_directories
from .pipeline import Pipeline
from .providers import AIProvider, ProviderError
from .speech_engine import MODELS
from .subtitles import SrtValidationError, SrtValidationResult, parse_srt_file
from .thumbnails import export_thumbnail
from .transcription import SpeechProgress
from .ui.caption_library import CaptionLibrary
from .ui.clips_view import ExportThumbnailDialog, FramePickerDialog, ThumbnailTaskRunner
from .ui.clips_workspace import ClipDetailPlayerPanel, ClipLibraryPanel
from .ui.dependency_view import DependencyManagerView
from .ui.download_view import DownloadView
from .ui.local_preview import LocalVideoDropZone, LocalVideoMetadata, LocalVideoPreview
from .ui.log_view import LogView
from .ui.performance_view import PerformanceView
from .ui.queue_workspace import (
    ActiveJobCard, CollapsibleLogsPanel, JobDetailsDialog, JobQueuePanel,
    QueueOverviewBar, project_thumbnail_path,
)
from .ui.speech_view import SpeechProgressView, SpeechSettingsView
from .ui.style import STYLESHEET, configure_form_layout, configure_input
from .ui.youtube_preview import YouTubePreview
from .worker import FunctionThread, PipelineWorker
from .youtube_metadata import YouTubeMetadata


class MainWindow(QMainWindow):
    NAV_ITEMS = ["Dashboard", "New Project", "Processing Queue", "Generated Clips", "Caption Templates",
                 "AI Provider Settings", "Speech-to-Text", "Performance", "Dependencies"]

    def __init__(self) -> None:
        super().__init__()
        ensure_directories()
        self.settings = SettingsStore()
        self.database = Database()
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
        self.closing = False
        self.thumb_runner = ThumbnailTaskRunner(self.settings.performance().profile, self)
        self.thumb_runner.completed.connect(self._on_thumbnail_ready)
        self.thumb_runner.failed.connect(self._on_thumbnail_failed)
        self.setWindowTitle("AutoClip Studio")
        if (app := QApplication.instance()) is not None and not app.windowIcon().isNull():
            self.setWindowIcon(app.windowIcon())
        elif (icon_path := asset_path("autoclip.ico")).is_file():
            self.setWindowIcon(QIcon(str(icon_path)))
        self.resize(1280, 820)
        self.setMinimumSize(760, 560)
        self.setStyleSheet(STYLESHEET)
        self._build_ui()

    def _build_ui(self) -> None:
        root = QWidget()
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        sidebar = QFrame(objectName="sidebar")
        sidebar.setFixedWidth(192)
        side_layout = QVBoxLayout(sidebar)
        side_layout.setContentsMargins(10, 16, 10, 16)
        brand = self._build_brand_header(172)
        side_layout.addWidget(brand)
        side_layout.addSpacing(12)
        self.nav_buttons: list[QPushButton] = []
        for index, name in enumerate(self.NAV_ITEMS):
            button = QPushButton(name, objectName="nav")
            button.setCheckable(True)
            button.clicked.connect(lambda checked=False, page=index: self.show_page(page))
            side_layout.addWidget(button)
            self.nav_buttons.append(button)
        side_layout.addStretch()
        version = QLabel("Local-first • No watermarks\nv1.0.0", objectName="muted")
        side_layout.addWidget(version)
        self.stack = QStackedWidget()
        self.stack.addWidget(self._dashboard_page())
        self.stack.addWidget(self._new_project_page())
        self.stack.addWidget(self._queue_page())
        self.stack.addWidget(self._clips_page())
        self.stack.addWidget(self._templates_page())
        self.stack.addWidget(self._scroll_page(self._settings_page()))
        self.stack.addWidget(self._scroll_page(self._speech_settings_page()))
        self.stack.addWidget(self._scroll_page(self._performance_page()))
        self.stack.addWidget(self._scroll_page(self._dependencies_page()))
        layout.addWidget(sidebar)
        layout.addWidget(self.stack, 1)
        self.setCentralWidget(root)
        self.show_page(0)

    def _build_brand_header(self, target_width: int) -> QLabel:
        brand = QLabel(objectName="brand")
        brand.setAccessibleName("AutoClip Studio")
        brand.setToolTip("AutoClip Studio")
        brand.setAlignment(Qt.AlignmentFlag.AlignCenter)
        wordmark_path = asset_path("AutoClipTrans.png")
        pixmap = QPixmap(str(wordmark_path)) if wordmark_path.is_file() else QPixmap()
        if not pixmap.isNull():
            dpr = max(1.0, self.devicePixelRatioF())
            scaled = pixmap.scaledToWidth(
                int(round(target_width * dpr)),
                Qt.TransformationMode.SmoothTransformation,
            )
            scaled.setDevicePixelRatio(dpr)
            brand.setPixmap(scaled)
            brand.setFixedHeight(max(44, int(round(scaled.height() / dpr))))
        else:
            brand.setText("AutoClip Studio")
            brand.setWordWrap(True)
        return brand

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
        form.setContentsMargins(4, 6, 4, 6)
        source_group = QGroupBox("Source video")
        source_layout = QVBoxLayout(source_group)

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

        self.srt_panel = QFrame(objectName="card")
        srt_layout = QVBoxLayout(self.srt_panel)
        srt_layout.setContentsMargins(12, 10, 12, 10)
        srt_layout.setSpacing(8)
        srt_row = QHBoxLayout()
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
        name_row.addRow("&Project name", self.project_name)
        name_row.labelForField(self.project_name).setObjectName("projectNameLabel")
        source_layout.addLayout(name_row)
        form.addWidget(source_group)
        settings_group = QGroupBox("Clip settings")
        grid = QGridLayout(settings_group)
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(12)
        self.clip_count = configure_input(self._spin(1, 10, 3))
        self.min_duration = configure_input(self._spin(10, 120, 25, " sec"))
        self.max_duration = configure_input(self._spin(15, 180, 60, " sec"))
        self.aspect = configure_input(QComboBox(), min_contents_length=8); self.aspect.addItems(["9:16", "1:1", "4:5", "16:9"])
        self.resolution = configure_input(QComboBox(), min_contents_length=12); self.resolution.addItems(["1080x1920", "720x1280", "1080x1080", "1080x1350", "1920x1080"])
        self.aspect.currentTextChanged.connect(self._aspect_changed)
        grid.addWidget(QLabel("Number of clips"), 0, 0); grid.addWidget(self.clip_count, 0, 1)
        grid.addWidget(QLabel("Minimum duration"), 0, 2); grid.addWidget(self.min_duration, 0, 3)
        grid.addWidget(QLabel("Maximum duration"), 1, 0); grid.addWidget(self.max_duration, 1, 1)
        grid.addWidget(QLabel("Aspect ratio"), 1, 2); grid.addWidget(self.aspect, 1, 3)
        grid.addWidget(QLabel("Resolution"), 2, 0); grid.addWidget(self.resolution, 2, 1)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        form.addWidget(settings_group)
        automation = QGroupBox("Automation")
        auto_layout = QGridLayout(automation)
        auto_layout.setHorizontalSpacing(16)
        auto_layout.setVerticalSpacing(10)
        self.face_tracking = QCheckBox("Auto face tracking"); self.face_tracking.setChecked(True)
        self.captions = QCheckBox("Animated word captions"); self.captions.setChecked(True)
        self.cache = QCheckBox("Cache downloads and transcripts"); self.cache.setChecked(True)
        self.template_combo = configure_input(QComboBox())
        self.template_combo.addItems(TEMPLATES.keys())
        self.template_combo.setCurrentText(self.default_template())
        auto_layout.addWidget(self.face_tracking, 0, 0)
        auto_layout.addWidget(self.captions, 0, 1)
        auto_layout.addWidget(self.cache, 1, 0, 1, 2)
        auto_layout.addWidget(QLabel("Caption template"), 2, 0)
        auto_layout.addWidget(self.template_combo, 2, 1)
        auto_layout.setColumnStretch(0, 1)
        auto_layout.setColumnStretch(1, 1)
        form.addWidget(automation)
        ai_group = QGroupBox("AI and transcription")
        ai_form = configure_form_layout(QFormLayout(ai_group))
        self.project_provider = configure_input(QComboBox()); self.project_provider.addItems(["gemini", "openai", "custom"])
        self.project_provider.setCurrentText(self.settings.data["active_provider"])
        self.project_model = configure_input(QLineEdit(self.settings.provider().get("model", "")))
        self.multimodal_check = QCheckBox("Optional Gemini video analysis (uploads source)")
        self.whisper_model = configure_input(QComboBox()); self.whisper_model.addItems(MODELS); self.whisper_model.setCurrentText(self.settings.data["whisper_model"])
        self.whisper_device = configure_input(QComboBox()); self.whisper_device.addItems(["auto", "cuda", "cpu"]); self.whisper_device.setCurrentText(self.settings.data["whisper_device"])
        self.project_provider.currentTextChanged.connect(self._project_provider_changed)
        ai_form.addRow("Provider", self.project_provider)
        ai_form.addRow("Model", self.project_model)
        ai_form.addRow("Multimodal", self.multimodal_check)
        ai_form.addRow("Whisper model", self.whisper_model)
        ai_form.addRow("Whisper device", self.whisper_device)
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

        self.active_job_card = ActiveJobCard(self.cancel_processing)
        self.active_job_card.new_project_requested.connect(lambda: self.show_page(1))
        page.addWidget(self.active_job_card)

        self.processing_status_row = self.active_job_card.processing_status_row
        self.processing_progress_row = self.active_job_card.processing_progress_row
        self.queue_status = self.active_job_card.queue_status
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
        split = QHBoxLayout()
        split.setSpacing(16)
        self.clips_library = ClipLibraryPanel()
        self.clips_library.clip_selected.connect(self._on_clip_selected)
        self.clips_library.export_all_requested.connect(self.export_all_clips)
        split.addWidget(self.clips_library, 3)

        self.clip_detail = ClipDetailPlayerPanel()
        self.player = self.clip_detail.player
        self.clip_detail.open_file_requested.connect(self.open_selected_clip)
        self.clip_detail.open_folder_requested.connect(self.open_selected_clip_folder)
        self.clip_detail.regenerate_thumb_requested.connect(self.regenerate_thumbnail)
        self.clip_detail.select_frame_requested.connect(self.select_thumbnail_frame)
        self.clip_detail.export_thumb_requested.connect(self.export_selected_thumbnail)
        self.clip_detail.rename_clip_requested.connect(self.rename_selected_clip)
        self.clip_detail.delete_clip_requested.connect(self.delete_selected_clip)
        split.addWidget(self.clip_detail, 2)
        page.addLayout(split, 1)
        return outer

    def _templates_page(self) -> QWidget:
        outer, page = self._page("Caption Templates", "Preview animated captions and choose a default for new projects.")
        self.caption_library = CaptionLibrary(self.default_template(), self.choose_template, lambda: self.show_page(1))
        self.caption_library.idle.connect(self._finish_close)
        page.addWidget(self.caption_library, 1)
        return outer

    def _settings_page(self) -> QWidget:
        outer, page = self._page("AI Provider Settings", f"Credentials are stored in {credential_store_name()}")
        group = self.provider_group = QGroupBox("Provider connection")
        form = configure_form_layout(QFormLayout(group))
        self.settings_provider = configure_input(QComboBox()); self.settings_provider.addItems(["gemini", "openai", "custom"])
        self.settings_provider.setCurrentText(self.settings.data["active_provider"])
        self.api_key_input = configure_input(QLineEdit()); self.api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.base_url_input = configure_input(QLineEdit())
        self.model_input = configure_input(QComboBox(), min_contents_length=24); self.model_input.setEditable(True)
        self.fallback_models_input = configure_input(QLineEdit())
        self.fallback_models_input.setPlaceholderText("Comma-separated model IDs, in retry order")
        self.auto_fallback_check = QCheckBox("Automatically try fallback models")
        self.auto_fallback_check.toggled.connect(self.fallback_models_input.setEnabled)
        self.settings_provider.currentTextChanged.connect(self.load_provider_form)
        form.addRow("Provider", self.settings_provider)
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
        local = QGroupBox("Local processing")
        local_form = configure_form_layout(QFormLayout(local))
        self.output_input = configure_input(QLineEdit(self.settings.data.get("output_dir", "")))
        self.output_input.setAccessibleName("Output folder")
        output_row = QHBoxLayout(); output_row.setSpacing(8); output_row.addWidget(self.output_input, 1)
        output_browse = QPushButton("Browse"); output_browse.clicked.connect(self.browse_output); output_row.addWidget(output_browse)
        hw_label = "Use Apple VideoToolbox when available" if sys.platform == "darwin" else "Use NVENC when available"
        self.hardware_check = QCheckBox(hw_label); self.hardware_check.setChecked(bool(self.settings.data.get("hardware_encoding", True)))
        local_form.addRow("Output folder", output_row); local_form.addRow("Encoding", self.hardware_check)
        local_actions = QHBoxLayout()
        local_actions.setSpacing(10)
        save_local = QPushButton("Save local settings"); save_local.clicked.connect(self.save_local_settings)
        local_actions.addWidget(save_local)
        local_actions.addStretch()
        local_form.addRow(local_actions)
        page.addWidget(local)
        self.connection_status = self._wrapping_label("", object_name="muted")
        self.connection_status.setTextFormat(Qt.TextFormat.PlainText)
        page.addWidget(self.connection_status)
        page.addStretch()
        self.load_provider_form(self.settings_provider.currentText())
        return outer

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
        outer, page = self._page("Dependency Manager", "Inspect, repair, and manage runtimes, tools, and Whisper models")
        self.dependency_view = DependencyManagerView(self.settings.speech)
        page.addWidget(self.dependency_view, 1)
        return outer

    def _save_performance_settings(self, perf) -> None:
        self.settings.data["performance"] = asdict(perf)
        self.settings.save()

    def show_page(self, index: int) -> None:
        if self.stack.currentIndex() == 3 and index != 3 and hasattr(self, "clip_detail"):
            self.clip_detail.stop_playback()
        if self.stack.currentIndex() == 4 and index != 4:
            self.caption_library.stop()
        if hasattr(self, "performance_view"):
            self.performance_view.set_polling(index == 7)
        self.stack.setCurrentIndex(index)
        for button_index, button in enumerate(self.nav_buttons):
            button.setChecked(button_index == index)
        if index in {0, 2, 3}:
            self.refresh_all()

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
        if not raw_path:
            self._srt_result = None
            self._srt_error = "Choose an .srt file or uncheck 'Import existing subtitle (.srt)'."
            self.srt_status.setStyleSheet("color: #A3A8B5;")
            self.srt_status.setText("Select an .srt file to validate cues and language.")
            return
        try:
            result = parse_srt_file(Path(raw_path), offset_seconds=self.srt_offset.value())
            self._srt_result = result
            self._srt_error = ""
            self.srt_status.setStyleSheet("color: #2ECC71;")
            self.srt_status.setText(
                f"✓ Valid SRT: {Path(raw_path).name}  •  {result.cue_count} cues  •  "
                f"{result.language_label}  •  {result.encoding}"
            )
        except SrtValidationError as error:
            self._srt_result = None
            self._srt_error = str(error)
            self.srt_status.setStyleSheet("color: #FF9AA2;")
            self.srt_status.setText(f"Invalid SRT: {error}")

    def browse_output(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Choose output folder", self.output_input.text() or str(OUTPUT_DIR))
        if path: self.output_input.setText(path)

    @Slot(object)
    def _update_project_name(self, metadata: YouTubeMetadata | None) -> None:
        suggestion = metadata.title if metadata else ""
        if self.project_name.text() in ("", self._auto_project_name):
            self.project_name.setText(suggestion)
        self._auto_project_name = suggestion

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
        options = ProjectOptions(
            source=source, clip_count=self.clip_count.value(), min_duration=self.min_duration.value(),
            max_duration=self.max_duration.value(), aspect_ratio=self.aspect.currentText(),
            resolution=self.resolution.currentText(), face_tracking=self.face_tracking.isChecked(),
            captions=self.captions.isChecked(), caption_template=self.template_combo.currentText(),
            provider=self.project_provider.currentText(), model=self.project_model.text().strip(),
            multimodal=self.multimodal_check.isChecked(),
            cache_enabled=self.cache.isChecked(), whisper_model=self.whisper_model.currentText(),
            whisper_device=self.whisper_device.currentText(), language=self.settings.speech().language or None,
            output_dir=self.settings.data.get("output_dir", ""),
            project_name=self.project_name.text().strip() or default_name,
            youtube_metadata=asdict(metadata) if metadata else None,
            srt_path=srt_path,
            srt_offset_seconds=srt_offset,
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

    def _start_worker(self, project_id: str, options: ProjectOptions, *, resume_analysis: bool = False) -> None:
        self.pipeline.manager.reset()
        self._active_project_id = project_id
        self.worker_thread = QThread(self)
        self.worker = PipelineWorker(self.pipeline, project_id, options, resume_analysis=resume_analysis)
        self.worker.moveToThread(self.worker_thread)
        self.worker_thread.started.connect(self.worker.run)
        self.worker.progress.connect(self.on_progress)
        self.worker.download_progress.connect(self.on_download_progress)
        self.worker.download_log.connect(self.on_download_log)
        self.worker.speech_progress.connect(self.on_speech_progress)
        self.worker.speech_log.connect(self.on_download_log)
        self.worker.warning.connect(self._provider_warning)
        self.worker.completed.connect(self.on_completed)
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
        options.model = self.settings.provider(options.provider).get("model", "")
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
            if clips and hasattr(self, "clips_library"):
                self.clips_library.select_clip(str(clips[0]["id"]))
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
        elif action == "retry_analysis":
            self.queue_panel.select_project(project_id)
            self.retry_analysis()
        elif action == "retry_failed":
            try:
                options = ProjectOptions(**json.loads(project["options"]))
            except (ValueError, TypeError):
                QMessageBox.warning(self, "Cannot retry job", "This project's saved settings could not be read.")
                return
            self.database.update_project(project_id, status="queued", progress=0, error="", stage="Queued")
            self.refresh_all()
            if not self.worker_thread and not self.queue_paused and not self.speech_utility_active:
                self._start_worker(project_id, options)
        elif action == "remove_history":
            if project.get("status") == "processing":
                return
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

    def cancel_processing(self) -> None:
        downloading = not self.download_view.isHidden()
        transcribing = not self.speech_progress.isHidden()
        button = (self.download_view.cancel_button if downloading else
                  self.speech_progress.cancel_button if transcribing else self.cancel_button)
        if self.worker and button.isEnabled():
            button.setEnabled(False)
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
        visible = self.download_view.isHidden() and self.speech_progress.isHidden()
        self.processing_status_row.setVisible(visible)
        self.processing_progress_row.setVisible(visible)

    @Slot(object)
    def on_download_progress(self, progress: DownloadProgress) -> None:
        if not self.download_view.isHidden() and not self.pipeline.manager.cancel_event.is_set():
            self.download_view.update_transfer(progress)

    @Slot(str)
    def on_download_log(self, message: str) -> None:
        self._log("INFO", message)

    @Slot(object)
    def on_speech_progress(self, progress: SpeechProgress) -> None:
        if not self.speech_progress.isHidden() and not self.pipeline.manager.cancel_event.is_set():
            self.speech_progress.update_transcription(progress)

    def on_progress(self, value: int, message: str) -> None:
        if self.pipeline.manager.cancel_event.is_set():
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

    def on_completed(self, _project_id: str, count: int) -> None:
        self.show_download(False)
        self.show_speech(False)
        self.active_job_card.mark_finished("completed", f"Completed {count} clips")
        self._log("SUCCESS", "All outputs passed MP4 validation.")
        self.refresh_all()

    def on_failed(self, project_id: str, message: str) -> None:
        self.show_download(False)
        self.show_speech(False)
        self.refresh_all()
        cancelled = message == "Processing cancelled"
        retryable = False
        for project in self.project_rows or []:
            if project["id"] == project_id and project["status"] == "analysis_failed":
                self.queue_panel.select_project(project_id)
                retryable = True
                break
        summary = message.partition("\n")[0]
        display_msg = f"{summary}\nSelect Retry AI Analysis to continue." if retryable else summary
        self.active_job_card.mark_finished("cancelled" if cancelled else ("analysis_failed" if retryable else "failed"), display_msg)
        self._log("WARNING" if cancelled else "ERROR", message)
        state = "Cancelled" if cancelled else "Error"
        self.metric_active.findChild(QLabel, "metricValue").setText(state)
        if not cancelled and not retryable and not self.closing:
            QMessageBox.critical(self, "Processing failed", message)

    def _worker_finished(self) -> None:
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
        self.clips_library.set_clips(clips, busy_ids=busy_ids)
        self.metric_clips.findChild(QLabel, "metricValue").setText(str(self.database.clip_count()))
        self.clip_rows = clips
        self.preview_selected()

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
            folder = Path(clip["path"]).parent
            if folder.exists():
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

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
        if not clip:
            return
        if QMessageBox.question(
            self, "Delete clip",
            f"Delete '{clip['title']}' and its thumbnail files?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        self.clip_detail.stop_playback()
        self.player.setSource(QUrl())
        removed = self.database.delete_clip(clip["id"])
        if removed:
            for key in ("path", "thumbnail_path", "thumbnail_raw_path"):
                target = Path(removed.get(key) or "")
                if target.is_file():
                    try:
                        target.unlink()
                    except OSError:
                        pass
        self.clip_rows = None
        self.refresh_all()

    def toggle_play(self) -> None:
        self.clip_detail.toggle_play()

    def _selected_clip(self) -> dict | None:
        return self.clips_library.selected_clip()

    def open_selected_clip(self) -> None:
        clip = self._selected_clip()
        if clip: QDesktopServices.openUrl(QUrl.fromLocalFile(clip["path"]))

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

    def _project_provider_changed(self, name: str) -> None:
        self.project_model.setText(self.settings.provider(name).get("model", ""))
        self.multimodal_check.setEnabled(name == "gemini")
        if name != "gemini": self.multimodal_check.setChecked(False)

    def _aspect_changed(self, ratio: str) -> None:
        recommended = {"9:16": "1080x1920", "1:1": "1080x1080", "4:5": "1080x1350", "16:9": "1920x1080"}
        self.resolution.setCurrentText(recommended.get(ratio, "1080x1920"))

    def load_provider_form(self, name: str) -> None:
        values = self.settings.provider(name)
        self.api_key_input.setText(self.settings.api_key(name))
        self.base_url_input.setText(values.get("base_url", ""))
        self.model_input.clear(); self.model_input.addItem(values.get("model", ""))
        self.fallback_models_input.setText(", ".join(values.get("fallback_models", [])))
        enabled = bool(values.get("auto_fallback"))
        self.auto_fallback_check.setChecked(enabled)
        self.fallback_models_input.setEnabled(enabled)

    def _fallback_models(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(
            model.strip() for model in self.fallback_models_input.text().split(",") if model.strip()
        ))

    def save_provider(self) -> None:
        name = self.settings_provider.currentText()
        self.settings.data["active_provider"] = name
        self.settings.data["providers"][name] = {
            "provider": name, "base_url": self.base_url_input.text().strip(),
            "model": self.model_input.currentText().strip(),
            "fallback_models": list(self._fallback_models()),
            "auto_fallback": self.auto_fallback_check.isChecked(),
        }
        try:
            self.settings.set_api_key(name, self.api_key_input.text().strip()); self.settings.save()
        except Exception as error:
            QMessageBox.critical(self, "Credential error", redact(str(error), self.api_key_input.text())); return
        self.project_provider.setCurrentText(name); self._project_provider_changed(name)
        self.connection_status.setText("Settings saved securely.")

    def _form_provider(self) -> AIProvider:
        return AIProvider(
            self.settings_provider.currentText(), self.api_key_input.text().strip(),
            self.base_url_input.text().strip(), self.model_input.currentText().strip(),
            fallback_models=self._fallback_models(), auto_fallback=self.auto_fallback_check.isChecked(),
            cancel_event=self.utility_cancel,
        )

    def test_provider(self) -> None:
        provider = self._form_provider(); self.connection_status.setText("Testing connection…")
        self._run_utility(provider, provider.test, lambda result: self.connection_status.setText(f"Connected: {result}"))

    def load_models(self) -> None:
        provider = self._form_provider(); self.connection_status.setText("Loading models…")
        def done(models: object) -> None:
            values = list(models); current = self.model_input.currentText(); self.model_input.clear(); self.model_input.addItems(values)
            if current in values: self.model_input.setCurrentText(current)
            self.connection_status.setText(f"Loaded {len(values)} models.")
        self._run_utility(provider, provider.list_models, done)

    def _speech_settings_page(self) -> QWidget:
        outer, layout = self._page("Speech-to-Text", "Configure local models, transcription quality and acceleration")
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
        self.settings.save(); self.connection_status.setText("Local settings saved.")

    def closeEvent(self, event: QCloseEvent) -> None:
        if not self.closing:
            self.closing = True
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
