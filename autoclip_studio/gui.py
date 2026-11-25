from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path
from threading import Event
from typing import Callable

from PySide6.QtCore import QFileInfo, QSignalBlocker, QSize, QThread, Qt, QUrl, Slot
from PySide6.QtGui import QCloseEvent, QDesktopServices, QIcon, QImage, QPixmap
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFileIconProvider, QFormLayout, QFrame, QGridLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QProgressBar, QPushButton, QScrollArea, QSizePolicy, QSpinBox,
    QStackedWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .captions import TEMPLATE_DEFAULT, TEMPLATES
from .config import SettingsStore
from .database import Database
from .diagnostics import LOG, SUCCESS, log_error, redact
from .media import DownloadProgress, is_youtube_url, youtube_id, youtube_source_error
from .models import ProjectOptions
from .paths import APP_ROOT, OUTPUT_DIR, asset_path, ensure_directories
from .pipeline import Pipeline
from .providers import AIProvider, ProviderError
from .speech_engine import MODELS
from .thumbnails import export_thumbnail
from .transcription import SpeechProgress
from .ui.caption_library import CaptionLibrary
from .ui.clips_view import (
    ClipThumbnailPreview, ExportThumbnailDialog, FramePickerDialog, ThumbnailTaskRunner,
)
from .ui.download_view import DownloadView
from .ui.log_view import LogView
from .ui.performance_view import PerformanceView
from .ui.speech_view import SpeechProgressView, SpeechSettingsView
from .ui.style import STYLESHEET
from .ui.youtube_preview import YouTubePreview
from .worker import FunctionThread, PipelineWorker
from .youtube_metadata import YouTubeMetadata


class MainWindow(QMainWindow):
    NAV_ITEMS = ["Dashboard", "New Project", "Processing Queue", "Generated Clips", "Caption Templates",
                 "AI Provider Settings", "Speech-to-Text", "Performance"]

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
        page.setContentsMargins(20, 20, 20, 20)
        page.setSpacing(16)
        page.addWidget(self._wrapping_label(title, object_name="pageTitle"))
        page.addWidget(self._wrapping_label(subtitle, object_name="muted"))
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
        self.metric_projects = self._metric_card("Projects", "0")
        self.metric_clips = self._metric_card("Generated clips", "0")
        self.metric_active = self._metric_card("Queue status", "Idle")
        for card in (self.metric_projects, self.metric_clips, self.metric_active):
            metrics.addWidget(card)
        page.addLayout(metrics)
        action_card = QFrame(objectName="card")
        action_layout = QVBoxLayout(action_card)
        action_layout.setContentsMargins(24, 24, 24, 24)
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
        source_row = QHBoxLayout()
        self.source_input = QLineEdit()
        self.source_input.setAccessibleName("Source video path or YouTube URL")
        self.source_input.setPlaceholderText("Paste a YouTube URL or choose a local video…")
        browse = QPushButton("Browse")
        browse.clicked.connect(self.browse_source)
        source_row.addWidget(self.source_input, 1)
        source_row.addWidget(browse)
        source_layout.addLayout(source_row)
        self.youtube_preview = YouTubePreview()
        self.youtube_preview.metadata_changed.connect(self._update_project_name)
        self.youtube_preview.idle.connect(self._finish_close)
        self.source_input.textChanged.connect(self.youtube_preview.set_source)
        source_layout.addWidget(self.youtube_preview)
        name_row = QFormLayout()
        self.project_name = QLineEdit(placeholderText="Optional project name")
        self._auto_project_name = ""
        name_row.addRow("&Project name", self.project_name)
        name_row.labelForField(self.project_name).setObjectName("projectNameLabel")
        source_layout.addLayout(name_row)
        form.addWidget(source_group)
        settings_group = QGroupBox("Clip settings")
        grid = QGridLayout(settings_group)
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(10)
        self.clip_count = self._spin(1, 10, 3)
        self.min_duration = self._spin(10, 120, 25, " sec")
        self.max_duration = self._spin(15, 180, 60, " sec")
        self.aspect = QComboBox(); self.aspect.addItems(["9:16", "1:1", "4:5", "16:9"])
        self.resolution = QComboBox(); self.resolution.addItems(["1080x1920", "720x1280", "1080x1080", "1080x1350", "1920x1080"])
        self.aspect.currentTextChanged.connect(self._aspect_changed)
        grid.addWidget(QLabel("Number of clips"), 0, 0); grid.addWidget(self.clip_count, 0, 1)
        grid.addWidget(QLabel("Minimum duration"), 0, 2); grid.addWidget(self.min_duration, 0, 3)
        grid.addWidget(QLabel("Maximum duration"), 1, 0); grid.addWidget(self.max_duration, 1, 1)
        grid.addWidget(QLabel("Aspect ratio"), 1, 2); grid.addWidget(self.aspect, 1, 3)
        grid.addWidget(QLabel("Resolution"), 2, 0); grid.addWidget(self.resolution, 2, 1)
        form.addWidget(settings_group)
        automation = QGroupBox("Automation")
        auto_layout = QGridLayout(automation)
        self.face_tracking = QCheckBox("Auto face tracking"); self.face_tracking.setChecked(True)
        self.captions = QCheckBox("Animated word captions"); self.captions.setChecked(True)
        self.cache = QCheckBox("Cache downloads and transcripts"); self.cache.setChecked(True)
        self.template_combo = QComboBox()
        self.template_combo.addItems(TEMPLATES.keys())
        self.template_combo.setCurrentText(self.default_template())
        auto_layout.addWidget(self.face_tracking, 0, 0)
        auto_layout.addWidget(self.captions, 0, 1)
        auto_layout.addWidget(self.cache, 1, 0, 1, 2)
        auto_layout.addWidget(QLabel("Caption template"), 2, 0)
        auto_layout.addWidget(self.template_combo, 2, 1)
        form.addWidget(automation)
        ai_group = QGroupBox("AI and transcription")
        ai_form = QFormLayout(ai_group)
        self.project_provider = QComboBox(); self.project_provider.addItems(["gemini", "openai", "custom"])
        self.project_provider.setCurrentText(self.settings.data["active_provider"])
        self.project_model = QLineEdit(self.settings.provider().get("model", ""))
        self.multimodal_check = QCheckBox("Optional Gemini video analysis (uploads source)")
        self.whisper_model = QComboBox(); self.whisper_model.addItems(MODELS); self.whisper_model.setCurrentText(self.settings.data["whisper_model"])
        self.whisper_device = QComboBox(); self.whisper_device.addItems(["auto", "cuda", "cpu"]); self.whisper_device.setCurrentText(self.settings.data["whisper_device"])
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
        outer, page = self._page("Processing Queue", "Heavy media tasks run one at a time")
        status_card = QFrame(objectName="card")
        layout = QVBoxLayout(status_card)
        self.processing_status_row = QWidget()
        row = QHBoxLayout(self.processing_status_row)
        row.setContentsMargins(0, 0, 0, 0)
        self.queue_status = self._wrapping_label("Idle")
        self.queue_status.setTextFormat(Qt.TextFormat.PlainText)
        self.cancel_button = QPushButton("Cancel processing", objectName="danger")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_processing)
        row.addWidget(self.queue_status, 1); row.addWidget(self.cancel_button)
        self.progress_bar = QProgressBar(); self.progress_bar.setRange(0, 100)
        self.progress_bar.setAccessibleName("Project progress")
        self.progress_text = QLabel("0%")
        self.download_view = DownloadView()
        self.download_view.cancel_button.clicked.connect(self.cancel_processing)
        self.download_view.hide()
        self.speech_progress = SpeechProgressView()
        self.speech_progress.cancel_button.clicked.connect(self.cancel_processing)
        self.speech_progress.hide()
        self.live_logs = LogView()
        log_actions = QHBoxLayout()
        copy_logs = QPushButton("Copy Logs")
        clear_logs = QPushButton("Clear Logs")
        export_logs = QPushButton("Export Logs")
        copy_logs.clicked.connect(self.live_logs.copy_all)
        clear_logs.clicked.connect(self.live_logs.clear)
        export_logs.clicked.connect(self.export_logs)
        for button in (copy_logs, clear_logs, export_logs):
            log_actions.addWidget(button)
        log_actions.addStretch()
        self.processing_progress_row = QWidget()
        progress_row = QHBoxLayout(self.processing_progress_row)
        progress_row.setContentsMargins(0, 0, 0, 0)
        progress_row.addWidget(self.progress_bar, 1)
        progress_row.addWidget(self.progress_text)
        layout.addWidget(self.processing_status_row)
        layout.addWidget(self.processing_progress_row)
        layout.addWidget(self.download_view)
        layout.addWidget(self.speech_progress)
        layout.addWidget(self.live_logs, 1)
        layout.addLayout(log_actions)
        page.addWidget(status_card, 2)
        history = QHBoxLayout()
        history.addWidget(QLabel("Project history", objectName="sectionTitle"), 1)
        self.retry_analysis_button = QPushButton("Retry AI Analysis")
        self.retry_analysis_button.setEnabled(False)
        self.retry_analysis_button.clicked.connect(self.retry_analysis)
        history.addWidget(self.retry_analysis_button)
        page.addLayout(history)
        self.queue_table = self._table(["Project", "Status", "Progress", "Error"])
        self.queue_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.queue_table.itemSelectionChanged.connect(self._update_retry_button)
        page.addWidget(self.queue_table, 1)
        return outer

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
        outer, page = self._page("Generated Clips", "Preview, inspect, and open finished exports")
        split = QHBoxLayout()
        self.clips_table = self._table(["Title", "Score", "Duration", "File"])
        self.clips_table.setAccessibleName("Generated clips")
        self.clips_table.setIconSize(QSize(34, 60))
        self.clips_table.verticalHeader().setDefaultSectionSize(66)
        self.clips_table.itemSelectionChanged.connect(self.preview_selected)
        split.addWidget(self.clips_table, 3)
        preview_card = QFrame(objectName="card")
        preview_layout = QVBoxLayout(preview_card)
        self.clip_preview_stack = QStackedWidget()
        self.clip_thumb_preview = ClipThumbnailPreview()
        self.video_widget = QVideoWidget(); self.video_widget.setMinimumSize(200, 235)
        self.clip_preview_stack.addWidget(self.clip_thumb_preview)
        self.clip_preview_stack.addWidget(self.video_widget)
        self.audio_output = QAudioOutput(); self.audio_output.setVolume(0.8)
        self.player = QMediaPlayer(); self.player.setAudioOutput(self.audio_output); self.player.setVideoOutput(self.video_widget)
        self.player.errorOccurred.connect(self._preview_error)
        self.player.playbackStateChanged.connect(self._playback_state_changed)
        controls = QHBoxLayout()
        self.play_button = QPushButton("Play / Pause"); self.play_button.clicked.connect(self.toggle_play)
        self.open_clip_button = QPushButton("Open file"); self.open_clip_button.clicked.connect(self.open_selected_clip)
        self.delete_clip_button = QPushButton("Delete", objectName="danger"); self.delete_clip_button.clicked.connect(self.delete_selected_clip)
        self.play_button.setEnabled(False)
        self.open_clip_button.setEnabled(False)
        self.delete_clip_button.setEnabled(False)
        controls.addWidget(self.play_button); controls.addWidget(self.open_clip_button); controls.addWidget(self.delete_clip_button)
        thumb_controls = QHBoxLayout()
        self.regen_thumb_button = QPushButton("Regenerate Thumbnail"); self.regen_thumb_button.clicked.connect(self.regenerate_thumbnail)
        self.pick_frame_button = QPushButton("Select Frame…"); self.pick_frame_button.clicked.connect(self.select_thumbnail_frame)
        self.export_thumb_button = QPushButton("Export Thumbnail…"); self.export_thumb_button.clicked.connect(self.export_selected_thumbnail)
        for btn in (self.regen_thumb_button, self.pick_frame_button, self.export_thumb_button):
            btn.setEnabled(False)
            thumb_controls.addWidget(btn)
        self.clip_meta = self._wrapping_label("Select a clip to preview", object_name="muted")
        preview_layout.addWidget(self.clip_preview_stack, 1)
        preview_layout.addLayout(controls)
        preview_layout.addLayout(thumb_controls)
        preview_layout.addWidget(self.clip_meta)
        split.addWidget(preview_card, 2)
        page.addLayout(split, 1)
        folder = QPushButton("Open output folder")
        folder.clicked.connect(self.open_output_folder)
        page.addWidget(folder, alignment=Qt.AlignmentFlag.AlignRight)
        return outer

    def _templates_page(self) -> QWidget:
        outer, page = self._page("Caption Templates", "Preview animated captions and choose a default for new projects.")
        self.caption_library = CaptionLibrary(self.default_template(), self.choose_template, lambda: self.show_page(1))
        self.caption_library.idle.connect(self._finish_close)
        page.addWidget(self.caption_library, 1)
        return outer

    def _settings_page(self) -> QWidget:
        outer, page = self._page("AI Provider Settings", "Credentials are stored in Windows Credential Manager")
        group = self.provider_group = QGroupBox("Provider connection")
        form = QFormLayout(group)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.settings_provider = QComboBox(); self.settings_provider.addItems(["gemini", "openai", "custom"])
        self.settings_provider.setCurrentText(self.settings.data["active_provider"])
        self.api_key_input = QLineEdit(); self.api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.base_url_input = QLineEdit()
        self.model_input = QComboBox(); self.model_input.setEditable(True)
        self.fallback_models_input = QLineEdit()
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
        local_form = QFormLayout(local)
        local_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.output_input = QLineEdit(self.settings.data.get("output_dir", ""))
        self.output_input.setAccessibleName("Output folder")
        output_row = QHBoxLayout(); output_row.addWidget(self.output_input)
        output_browse = QPushButton("Browse"); output_browse.clicked.connect(self.browse_output); output_row.addWidget(output_browse)
        self.hardware_check = QCheckBox("Use NVENC when available"); self.hardware_check.setChecked(bool(self.settings.data.get("hardware_encoding", True)))
        local_form.addRow("Output folder", output_row); local_form.addRow("Encoding", self.hardware_check)
        save_local = QPushButton("Save local settings"); save_local.clicked.connect(self.save_local_settings)
        local_form.addRow(save_local)
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

    def _save_performance_settings(self, perf) -> None:
        self.settings.data["performance"] = asdict(perf)
        self.settings.save()

    def show_page(self, index: int) -> None:
        if self.stack.currentIndex() == 4 and index != 4:
            self.caption_library.stop()
        if hasattr(self, "performance_view"):
            self.performance_view.set_polling(index == 7)
        self.stack.setCurrentIndex(index)
        for button_index, button in enumerate(self.nav_buttons):
            button.setChecked(button_index == index)
        if index in {0, 2, 3}:
            self.refresh_all()

    def browse_source(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose source video", "", "Videos (*.mp4 *.mkv *.mov *.webm *.avi);;All files (*)")
        if path: self.source_input.setText(path)

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
        if self.worker_thread or self.speech_utility_active:
            QMessageBox.information(self, "Processing active", "Wait for or cancel the current project first.")
            return
        source = self.source_input.text().strip()
        youtube = is_youtube_url(source)
        error = youtube_source_error(source) if youtube else ""
        if not youtube and (not source or not Path(source).is_file()):
            error = "Choose an existing video file or enter a valid YouTube URL."
        if error:
            QMessageBox.warning(self, "Invalid source", error)
            return
        if self.min_duration.value() > self.max_duration.value():
            QMessageBox.warning(self, "Invalid duration", "Minimum duration cannot exceed maximum duration.")
            return
        metadata = self.youtube_preview.result
        if metadata and (not youtube or metadata.video_id != youtube_id(source)):
            metadata = None
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
            project_name=self.project_name.text().strip() or (metadata.title if metadata else ""),
            youtube_metadata=asdict(metadata) if metadata else None,
        )
        project_id = self.database.create_project(source, asdict(options))
        self.template_combo.setCurrentText(self.default_template())
        self._start_worker(project_id, options)

    def _start_worker(self, project_id: str, options: ProjectOptions, *, resume_analysis: bool = False) -> None:
        self.pipeline.manager.reset()
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
        self.generate_button.setEnabled(False); self.cancel_button.setEnabled(True)
        self.retry_analysis_button.setEnabled(False)
        progress = 40 if resume_analysis else 0
        self.progress_bar.setValue(progress)
        self.progress_text.setText(f"{progress}%")
        self.show_speech(False)
        self.show_download(not resume_analysis and is_youtube_url(options.source))
        self.worker_thread.start(); self.show_page(2)

    def _selected_project(self) -> dict | None:
        rows = self.queue_table.selectionModel().selectedRows()
        return rows[0].data(Qt.ItemDataRole.UserRole) if rows else None

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
            self.download_view.reset()
        else:
            self.download_view.animation.stop()
        self.download_view.setVisible(active)
        self._update_processing_rows()

    def show_speech(self, active: bool) -> None:
        if active:
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
        self.live_logs.add_entry("INFO", message)

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
        if value:
            self.progress_bar.setValue(value)
            self.progress_text.setText(f"{value}%")
        self.queue_status.setText(redact(message).partition("\n")[0])
        self.live_logs.add_entry("INFO", message)
        self.metric_active.findChild(QLabel, "metricValue").setText(f"{value}%")

    def on_completed(self, _project_id: str, count: int) -> None:
        self.show_download(False)
        self.show_speech(False)
        self.queue_status.setText(f"Completed {count} clips")
        self.live_logs.add_entry("SUCCESS", "All outputs passed MP4 validation.")
        self.refresh_all()

    def on_failed(self, project_id: str, message: str) -> None:
        self.show_download(False)
        self.show_speech(False)
        self.refresh_all()
        cancelled = message == "Processing cancelled"
        retryable = False
        for index, project in enumerate(self.project_rows or []):
            if project["id"] == project_id and project["status"] == "analysis_failed":
                self.queue_table.selectRow(index)
                retryable = True
                break
        summary = message.partition("\n")[0]
        self.queue_status.setText(f"{summary}\nSelect Retry AI Analysis to continue." if retryable else summary)
        self.live_logs.add_entry("WARNING" if cancelled else "ERROR", message)
        state = "Cancelled" if cancelled else "Error"
        self.metric_active.findChild(QLabel, "metricValue").setText(state)
        if not cancelled and not retryable and not self.closing:
            QMessageBox.critical(self, "Processing failed", message)

    def _worker_finished(self) -> None:
        self.generate_button.setEnabled(True); self.cancel_button.setEnabled(False)
        if self.worker_thread:
            self.worker_thread.deleteLater()
        self.worker_thread = None; self.worker = None
        self._update_retry_button()
        self.metric_active.findChild(QLabel, "metricValue").setText("Idle")
        self._finish_close()

    def refresh_all(self) -> None:
        projects, clips = self.database.projects(), self.database.clips()
        if projects != self.project_rows:
            selected_project = self._selected_project()
            self._fill_projects(self.recent_table, projects[:8], compact=True)
            with QSignalBlocker(self.queue_table):
                self.queue_table.clearSelection()
                self._fill_projects(self.queue_table, projects, compact=False)
                if selected_project:
                    for row, item in enumerate(projects):
                        if item["id"] == selected_project["id"]:
                            self.queue_table.selectRow(row)
                            break
            self.metric_projects.findChild(QLabel, "metricValue").setText(str(self.database.project_count()))
            self.project_rows = projects
            self._update_retry_button()
        if clips == self.clip_rows:
            return
        selected = self._selected_clip()
        with QSignalBlocker(self.clips_table):
            self.clips_table.clearSelection()
            self.clips_table.setRowCount(len(clips))
            for row, clip in enumerate(clips):
                values = [clip["title"], str(clip["score"]), f"{clip['duration']:.1f}s", Path(clip["path"]).name]
                thumb_file = Path(clip.get("thumbnail_path") or "")
                clip_file = Path(clip["path"])
                if clip_file.is_file() and (not thumb_file.is_file()):
                    self.thumb_runner.submit(
                        clip["id"], clip_file, title=clip["title"],
                        profile=self.settings.performance().profile,
                        existing_cache_key=clip.get("thumbnail_cache_key", ""),
                    )
                for column, value in enumerate(values):
                    item = QTableWidgetItem(value)
                    if column == 0:
                        item.setData(Qt.ItemDataRole.UserRole, clip)
                        if thumb_file.is_file():
                            item.setIcon(QIcon(str(thumb_file)))
                    self.clips_table.setItem(row, column, item)
                if selected and clip["id"] == selected["id"]:
                    self.clips_table.selectRow(row)
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

    def preview_selected(self) -> None:
        clip = self._selected_clip()
        self.play_button.setEnabled(bool(clip))
        self.open_clip_button.setEnabled(bool(clip))
        self.delete_clip_button.setEnabled(bool(clip))
        for btn in (self.regen_thumb_button, self.pick_frame_button, self.export_thumb_button):
            btn.setEnabled(False)
        if not clip:
            self.player.setSource(QUrl())
            self.clip_preview_stack.setCurrentIndex(0)
            self.clip_thumb_preview.set_thumbnail(None, 0.0, status="Select a clip to preview")
            self.clip_meta.setText("Select a clip to preview")
            return
        clip_path = Path(clip["path"])
        if not clip_path.is_file():
            self.player.setSource(QUrl())
            self.play_button.setEnabled(False)
            self.open_clip_button.setEnabled(False)
            self.clip_preview_stack.setCurrentIndex(0)
            self.clip_thumb_preview.set_thumbnail(None, float(clip.get("duration") or 0.0), status="Clip file missing")
            self.clip_meta.setText("The exported clip file could not be found.")
            return
        for btn in (self.regen_thumb_button, self.pick_frame_button, self.export_thumb_button):
            btn.setEnabled(True)
        source = QUrl.fromLocalFile(clip["path"])
        if source != self.player.source():
            self.player.setSource(source)
            self.clip_preview_stack.setCurrentIndex(0)
        thumb_path = Path(clip.get("thumbnail_path") or "")
        busy = self.thumb_runner.is_busy(clip["id"])
        if thumb_path.is_file() and not busy:
            self.clip_thumb_preview.set_thumbnail(QImage(str(thumb_path)), float(clip["duration"]))
            thumb_status = "Thumbnail ready"
        elif busy:
            self.clip_thumb_preview.set_thumbnail(None, float(clip["duration"]), loading=True)
            thumb_status = "Generating thumbnail…"
        else:
            self.clip_thumb_preview.set_thumbnail(None, float(clip["duration"]), loading=True)
            thumb_status = "Thumbnail queued"
        self.clip_meta.setText(
            f"{clip['title']}\nScore {clip['score']}/100 • {clip['duration']:.1f}s • {thumb_status}\n{clip['description']}"
        )

    @Slot(QMediaPlayer.PlaybackState)
    def _playback_state_changed(self, state: QMediaPlayer.PlaybackState) -> None:
        if state == QMediaPlayer.PlaybackState.PlayingState:
            self.clip_preview_stack.setCurrentIndex(1)
        elif state == QMediaPlayer.PlaybackState.StoppedState:
            self.clip_preview_stack.setCurrentIndex(0)

    @Slot(str, object)
    def _on_thumbnail_ready(self, clip_id: str, result) -> None:
        self.database.update_clip_thumbnail(
            clip_id, str(result.preview_path), str(result.raw_path), result.timestamp, result.cache_key,
        )
        self.clip_rows = None
        self.refresh_all()

    @Slot(str, str)
    def _on_thumbnail_failed(self, clip_id: str, message: str) -> None:
        LOG.warning("Thumbnail generation failed for %s: %s", clip_id, message)
        selected = self._selected_clip()
        if selected and selected["id"] == clip_id:
            self.clip_thumb_preview.set_thumbnail(None, float(selected["duration"]), status="Thumbnail unavailable")

    def regenerate_thumbnail(self) -> None:
        clip = self._selected_clip()
        if not clip:
            return
        clip_path = Path(clip["path"])
        if not clip_path.is_file():
            return
        self.clip_preview_stack.setCurrentIndex(0)
        self.clip_thumb_preview.set_thumbnail(None, float(clip["duration"]), loading=True)
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
        self.clip_preview_stack.setCurrentIndex(0)
        self.clip_thumb_preview.set_thumbnail(None, float(clip["duration"]), loading=True)
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

    def _preview_error(self, _error: QMediaPlayer.Error, message: str) -> None:
        self.clip_meta.setText(f"Preview unavailable. {message}")

    def toggle_play(self) -> None:
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState: self.player.pause()
        else: self.player.play()

    def _selected_clip(self) -> dict | None:
        rows = self.clips_table.selectionModel().selectedRows()
        return rows[0].data(Qt.ItemDataRole.UserRole) if rows else None

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
            self.generate_button.setEnabled(self.worker_thread is None)
            self._update_retry_button()
        self._finish_close()

    def _finish_close(self) -> None:
        if (self.closing and not self.worker_thread and not self.utility_threads
                and not self.caption_library.busy and not self.youtube_preview.busy):
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
            self.pipeline.manager.cancel()
            self.utility_cancel.set()
        if self.worker_thread or self.utility_threads or self.caption_library.busy or self.youtube_preview.busy:
            self.setEnabled(False)
            self.setWindowTitle("AutoClip Studio — Shutting down…")
            event.ignore()
            return
        self.pipeline.monitor.shutdown()
        event.accept()


def run() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("AutoClip Studio")
    app.setOrganizationName("AutoClip")
    icon_path = asset_path("autoclip.ico")
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
