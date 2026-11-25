from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QThread, Qt, QUrl, Signal
from PySide6.QtGui import QCloseEvent, QDesktopServices
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout, QFrame, QGridLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QSlider, QSpinBox,
    QStackedWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .captions import TEMPLATES
from .config import SettingsStore
from .database import Database
from .media import is_youtube_url
from .models import ProjectOptions
from .paths import OUTPUT_DIR, ensure_directories
from .pipeline import Pipeline
from .providers import AIProvider
from .style import STYLESHEET
from .worker import PipelineWorker


class FunctionThread(QThread):
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, function: Callable[[], object], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.function = function

    def run(self) -> None:
        try:
            self.succeeded.emit(self.function())
        except Exception as error:
            self.failed.emit(str(error))


class MainWindow(QMainWindow):
    NAV_ITEMS = ["Dashboard", "New Project", "Processing Queue", "Generated Clips", "Caption Templates", "AI Provider Settings"]

    def __init__(self) -> None:
        super().__init__()
        ensure_directories()
        self.settings = SettingsStore()
        self.database = Database()
        self.pipeline = Pipeline(self.database, self.settings)
        self.worker_thread: QThread | None = None
        self.worker: PipelineWorker | None = None
        self.utility_threads: list[FunctionThread] = []
        self.setWindowTitle("AutoClip Studio")
        self.resize(1280, 820)
        self.setMinimumSize(1000, 680)
        self.setStyleSheet(STYLESHEET)
        self._build_ui()
        self.refresh_all()

    def _build_ui(self) -> None:
        root = QWidget()
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        sidebar = QFrame(objectName="sidebar")
        sidebar.setFixedWidth(220)
        side_layout = QVBoxLayout(sidebar)
        side_layout.setContentsMargins(12, 18, 12, 18)
        brand = QLabel("✦  AutoClip Studio", objectName="brand")
        side_layout.addWidget(brand)
        side_layout.addSpacing(20)
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
        self.stack.addWidget(self._settings_page())
        layout.addWidget(sidebar)
        layout.addWidget(self.stack, 1)
        self.setCentralWidget(root)
        self.show_page(0)

    def _page(self, title: str, subtitle: str) -> tuple[QWidget, QVBoxLayout]:
        outer = QWidget()
        page = QVBoxLayout(outer)
        page.setContentsMargins(30, 25, 30, 25)
        page.setSpacing(14)
        page.addWidget(QLabel(title, objectName="pageTitle"))
        page.addWidget(QLabel(subtitle, objectName="muted"))
        return outer, page

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
        action_layout.setContentsMargins(24, 22, 24, 22)
        action_layout.addWidget(QLabel("Create polished vertical clips from any long video"))
        action_layout.addWidget(QLabel("Import locally or paste a YouTube link. Transcription, selection, framing, captions, and rendering run in one workflow.", objectName="muted"))
        new_button = QPushButton("Start a new project", objectName="primary")
        new_button.clicked.connect(lambda: self.show_page(1))
        action_layout.addWidget(new_button, alignment=Qt.AlignmentFlag.AlignLeft)
        page.addWidget(action_card)
        page.addWidget(QLabel("Recent projects"))
        self.recent_table = self._table(["Source", "Status", "Progress", "Created"])
        page.addWidget(self.recent_table, 1)
        return outer

    def _metric_card(self, label: str, value: str) -> QFrame:
        card = QFrame(objectName="card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.addWidget(QLabel(label, objectName="muted"))
        value_label = QLabel(value, objectName="metric")
        value_label.setObjectName("metricValue")
        layout.addWidget(value_label)
        return card

    def _new_project_page(self) -> QWidget:
        outer, page = self._page("New Project", "Choose a source and configure your shorts")
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        form = QVBoxLayout(content)
        source_group = QGroupBox("Source video")
        source_layout = QVBoxLayout(source_group)
        source_row = QHBoxLayout()
        self.source_input = QLineEdit()
        self.source_input.setPlaceholderText("Paste a YouTube URL or choose a local video…")
        browse = QPushButton("Browse")
        browse.clicked.connect(self.browse_source)
        source_row.addWidget(self.source_input, 1)
        source_row.addWidget(browse)
        source_layout.addLayout(source_row)
        form.addWidget(source_group)
        settings_group = QGroupBox("Clip settings")
        grid = QGridLayout(settings_group)
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
        self.template_combo = QComboBox(); self.template_combo.addItems(TEMPLATES.keys())
        auto_layout.addWidget(self.face_tracking, 0, 0)
        auto_layout.addWidget(self.captions, 0, 1)
        auto_layout.addWidget(self.cache, 1, 0)
        auto_layout.addWidget(QLabel("Caption template"), 1, 1)
        auto_layout.addWidget(self.template_combo, 1, 2)
        form.addWidget(automation)
        ai_group = QGroupBox("AI and transcription")
        ai_form = QFormLayout(ai_group)
        self.project_provider = QComboBox(); self.project_provider.addItems(["gemini", "openai", "custom"])
        self.project_provider.setCurrentText(self.settings.data["active_provider"])
        self.project_model = QLineEdit(self.settings.provider().get("model", ""))
        self.multimodal_check = QCheckBox("Optional Gemini video analysis (uploads source)")
        self.whisper_model = QComboBox(); self.whisper_model.addItems(["tiny", "base", "small", "medium", "large-v3"]); self.whisper_model.setCurrentText(self.settings.data["whisper_model"])
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
        row = QHBoxLayout()
        self.queue_status = QLabel("Idle")
        self.cancel_button = QPushButton("Cancel processing", objectName="danger")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_processing)
        row.addWidget(self.queue_status); row.addStretch(); row.addWidget(self.cancel_button)
        self.progress_bar = QProgressBar(); self.progress_bar.setRange(0, 100)
        self.live_logs = QPlainTextEdit(); self.live_logs.setReadOnly(True); self.live_logs.setMaximumBlockCount(500)
        layout.addLayout(row); layout.addWidget(self.progress_bar); layout.addWidget(self.live_logs)
        page.addWidget(status_card, 2)
        page.addWidget(QLabel("Project history"))
        self.queue_table = self._table(["Source", "Status", "Progress", "Error"])
        page.addWidget(self.queue_table, 1)
        return outer

    def _clips_page(self) -> QWidget:
        outer, page = self._page("Generated Clips", "Preview, inspect, and open finished exports")
        split = QHBoxLayout()
        self.clips_table = self._table(["Title", "Score", "Duration", "File"])
        self.clips_table.itemSelectionChanged.connect(self.preview_selected)
        split.addWidget(self.clips_table, 3)
        preview_card = QFrame(objectName="card")
        preview_layout = QVBoxLayout(preview_card)
        self.video_widget = QVideoWidget(); self.video_widget.setMinimumSize(280, 360)
        self.audio_output = QAudioOutput(); self.audio_output.setVolume(0.8)
        self.player = QMediaPlayer(); self.player.setAudioOutput(self.audio_output); self.player.setVideoOutput(self.video_widget)
        controls = QHBoxLayout()
        self.play_button = QPushButton("Play / Pause"); self.play_button.clicked.connect(self.toggle_play)
        open_file = QPushButton("Open file"); open_file.clicked.connect(self.open_selected_clip)
        controls.addWidget(self.play_button); controls.addWidget(open_file)
        self.clip_meta = QLabel("Select a clip to preview", objectName="muted"); self.clip_meta.setWordWrap(True)
        preview_layout.addWidget(self.video_widget, 1); preview_layout.addLayout(controls); preview_layout.addWidget(self.clip_meta)
        split.addWidget(preview_card, 2)
        page.addLayout(split, 1)
        folder = QPushButton("Open output folder")
        folder.clicked.connect(self.open_output_folder)
        page.addWidget(folder, alignment=Qt.AlignmentFlag.AlignRight)
        return outer

    def _templates_page(self) -> QWidget:
        outer, page = self._page("Caption Templates", "Choose the animated subtitle style used for new projects")
        grid = QGridLayout()
        self.template_buttons: list[QPushButton] = []
        for index, (name, values) in enumerate(TEMPLATES.items()):
            card = QFrame(objectName="card")
            layout = QVBoxLayout(card)
            sample = QLabel("WORDS THAT POP")
            sample.setAlignment(Qt.AlignmentFlag.AlignCenter)
            sample.setMinimumHeight(110)
            sample.setStyleSheet(f"font-family: '{values['font']}'; font-size: 24px; font-weight: 800; color: white; background: #090b10; border-radius: 8px;")
            button = QPushButton(f"Use {name}")
            button.clicked.connect(lambda checked=False, selected=name: self.choose_template(selected))
            layout.addWidget(QLabel(name)); layout.addWidget(sample); layout.addWidget(button)
            grid.addWidget(card, index // 2, index % 2)
            self.template_buttons.append(button)
        page.addLayout(grid)
        page.addStretch()
        return outer

    def _settings_page(self) -> QWidget:
        outer, page = self._page("AI Provider Settings", "Credentials are stored in Windows Credential Manager")
        group = QGroupBox("Provider connection")
        form = QFormLayout(group)
        self.settings_provider = QComboBox(); self.settings_provider.addItems(["gemini", "openai", "custom"])
        self.settings_provider.setCurrentText(self.settings.data["active_provider"])
        self.api_key_input = QLineEdit(); self.api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.base_url_input = QLineEdit()
        self.model_input = QComboBox(); self.model_input.setEditable(True)
        self.settings_provider.currentTextChanged.connect(self.load_provider_form)
        form.addRow("Provider", self.settings_provider)
        form.addRow("API key", self.api_key_input)
        form.addRow("Base URL", self.base_url_input)
        form.addRow("Model", self.model_input)
        buttons = QHBoxLayout()
        save = QPushButton("Save settings", objectName="primary"); save.clicked.connect(self.save_provider)
        test = QPushButton("Test connection"); test.clicked.connect(self.test_provider)
        models = QPushButton("Load models"); models.clicked.connect(self.load_models)
        buttons.addWidget(save); buttons.addWidget(test); buttons.addWidget(models); buttons.addStretch()
        form.addRow(buttons)
        page.addWidget(group)
        local = QGroupBox("Local processing")
        local_form = QFormLayout(local)
        self.output_input = QLineEdit(self.settings.data.get("output_dir", ""))
        output_row = QHBoxLayout(); output_row.addWidget(self.output_input)
        output_browse = QPushButton("Browse"); output_browse.clicked.connect(self.browse_output); output_row.addWidget(output_browse)
        self.hardware_check = QCheckBox("Use NVENC when available"); self.hardware_check.setChecked(bool(self.settings.data.get("hardware_encoding", True)))
        local_form.addRow("Output folder", output_row); local_form.addRow("Encoding", self.hardware_check)
        save_local = QPushButton("Save local settings"); save_local.clicked.connect(self.save_local_settings)
        local_form.addRow(save_local)
        page.addWidget(local)
        self.connection_status = QLabel("", objectName="muted")
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

    def show_page(self, index: int) -> None:
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

    def generate(self) -> None:
        if self.worker_thread and self.worker_thread.isRunning():
            QMessageBox.information(self, "Processing active", "Wait for or cancel the current project first.")
            return
        source = self.source_input.text().strip()
        if not source or (not Path(source).is_file() and not is_youtube_url(source)):
            QMessageBox.warning(self, "Invalid source", "Choose an existing video file or enter a valid YouTube URL.")
            return
        if self.min_duration.value() > self.max_duration.value():
            QMessageBox.warning(self, "Invalid duration", "Minimum duration cannot exceed maximum duration.")
            return
        options = ProjectOptions(
            source=source, clip_count=self.clip_count.value(), min_duration=self.min_duration.value(),
            max_duration=self.max_duration.value(), aspect_ratio=self.aspect.currentText(),
            resolution=self.resolution.currentText(), face_tracking=self.face_tracking.isChecked(),
            captions=self.captions.isChecked(), caption_template=self.template_combo.currentText(),
            provider=self.project_provider.currentText(), model=self.project_model.text().strip(),
            multimodal=self.multimodal_check.isChecked(),
            cache_enabled=self.cache.isChecked(), whisper_model=self.whisper_model.currentText(),
            whisper_device=self.whisper_device.currentText(), output_dir=self.settings.data.get("output_dir", ""),
        )
        project_id = self.database.create_project(source, asdict(options))
        self.worker_thread = QThread(self)
        self.worker = PipelineWorker(self.pipeline, project_id, options)
        self.worker.moveToThread(self.worker_thread)
        self.worker_thread.started.connect(self.worker.run)
        self.worker.progress.connect(self.on_progress)
        self.worker.completed.connect(self.on_completed)
        self.worker.failed.connect(self.on_failed)
        self.worker.finished.connect(self.worker_thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.worker_thread.finished.connect(self._worker_finished)
        self.generate_button.setEnabled(False); self.cancel_button.setEnabled(True)
        self.live_logs.clear(); self.progress_bar.setValue(0)
        self.worker_thread.start(); self.show_page(2)

    def cancel_processing(self) -> None:
        if self.worker:
            self.queue_status.setText("Cancelling…")
            self.pipeline.manager.cancel()

    def on_progress(self, value: int, message: str) -> None:
        if value: self.progress_bar.setValue(value)
        self.queue_status.setText(message)
        self.live_logs.appendPlainText(message)
        self.metric_active.findChild(QLabel, "metricValue").setText(f"{value}%" if value else "Error")

    def on_completed(self, _project_id: str, count: int) -> None:
        self.queue_status.setText(f"Completed {count} clips")
        self.live_logs.appendPlainText("All outputs passed MP4 validation.")
        self.refresh_all()

    def on_failed(self, _project_id: str, message: str) -> None:
        self.queue_status.setText(message)
        self.live_logs.appendPlainText(message)
        if message != "Processing cancelled":
            QMessageBox.critical(self, "Processing failed", message)
        self.refresh_all()

    def _worker_finished(self) -> None:
        self.generate_button.setEnabled(True); self.cancel_button.setEnabled(False)
        if self.worker_thread:
            self.worker_thread.deleteLater()
        self.worker_thread = None; self.worker = None

    def refresh_all(self) -> None:
        projects, clips = self.database.projects(), self.database.clips()
        self._fill_projects(self.recent_table, projects[:8], compact=True)
        self._fill_projects(self.queue_table, projects, compact=False)
        self.metric_projects.findChild(QLabel, "metricValue").setText(str(len(projects)))
        self.metric_clips.findChild(QLabel, "metricValue").setText(str(len(clips)))
        if not (self.worker_thread and self.worker_thread.isRunning()):
            self.metric_active.findChild(QLabel, "metricValue").setText("Idle")
        self.clips_table.setRowCount(len(clips))
        for row, clip in enumerate(clips):
            values = [clip["title"], str(clip["score"]), f"{clip['duration']:.1f}s", Path(clip["path"]).name]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value); item.setData(Qt.ItemDataRole.UserRole, clip)
                self.clips_table.setItem(row, column, item)

    def _fill_projects(self, table: QTableWidget, projects: list[dict], compact: bool) -> None:
        table.setRowCount(len(projects))
        for row, project in enumerate(projects):
            source = Path(project["source"]).name if not is_youtube_url(project["source"]) else project["source"]
            values = ([source, project["status"], f"{project['progress']}%", project["created_at"][:16].replace("T", " ")]
                      if compact else [source, project["status"], f"{project['progress']}%", project["error"]])
            for column, value in enumerate(values): table.setItem(row, column, QTableWidgetItem(str(value)))

    def preview_selected(self) -> None:
        items = self.clips_table.selectedItems()
        if not items: return
        clip = items[0].data(Qt.ItemDataRole.UserRole)
        self.player.setSource(QUrl.fromLocalFile(clip["path"]))
        self.clip_meta.setText(f"{clip['title']}\nScore {clip['score']}/100 • {clip['duration']:.1f}s\n{clip['description']}")

    def toggle_play(self) -> None:
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState: self.player.pause()
        else: self.player.play()

    def _selected_clip(self) -> dict | None:
        items = self.clips_table.selectedItems()
        return items[0].data(Qt.ItemDataRole.UserRole) if items else None

    def open_selected_clip(self) -> None:
        clip = self._selected_clip()
        if clip: QDesktopServices.openUrl(QUrl.fromLocalFile(clip["path"]))

    def open_output_folder(self) -> None:
        path = Path(self.settings.data.get("output_dir") or OUTPUT_DIR)
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def choose_template(self, name: str) -> None:
        self.template_combo.setCurrentText(name); self.show_page(1)

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

    def save_provider(self) -> None:
        name = self.settings_provider.currentText()
        self.settings.data["active_provider"] = name
        self.settings.data["providers"][name] = {"provider": name, "base_url": self.base_url_input.text().strip(), "model": self.model_input.currentText().strip()}
        try:
            self.settings.set_api_key(name, self.api_key_input.text().strip()); self.settings.save()
        except Exception as error:
            QMessageBox.critical(self, "Credential error", str(error)); return
        self.project_provider.setCurrentText(name); self._project_provider_changed(name)
        self.connection_status.setText("Settings saved securely.")

    def _form_provider(self) -> AIProvider:
        return AIProvider(self.settings_provider.currentText(), self.api_key_input.text().strip(), self.base_url_input.text().strip(), self.model_input.currentText().strip())

    def test_provider(self) -> None:
        provider = self._form_provider(); self.connection_status.setText("Testing connection…")
        self._run_utility(provider.test, lambda result: self.connection_status.setText(f"Connected: {result}"))

    def load_models(self) -> None:
        provider = self._form_provider(); self.connection_status.setText("Loading models…")
        def done(models: object) -> None:
            values = list(models); current = self.model_input.currentText(); self.model_input.clear(); self.model_input.addItems(values)
            if current in values: self.model_input.setCurrentText(current)
            self.connection_status.setText(f"Loaded {len(values)} models.")
        self._run_utility(provider.list_models, done)

    def _run_utility(self, function: Callable[[], object], done: Callable[[object], None]) -> None:
        thread = FunctionThread(function, self); self.utility_threads.append(thread)
        thread.succeeded.connect(done)
        thread.failed.connect(lambda message: self.connection_status.setText(f"Error: {message}"))
        thread.finished.connect(lambda: self.utility_threads.remove(thread) if thread in self.utility_threads else None)
        thread.start()

    def save_local_settings(self) -> None:
        self.settings.data["output_dir"] = self.output_input.text().strip()
        self.settings.data["hardware_encoding"] = self.hardware_check.isChecked()
        self.settings.save(); self.connection_status.setText("Local settings saved.")

    def closeEvent(self, event: QCloseEvent) -> None:
        self.player.stop(); self.pipeline.manager.cancel()
        if self.worker_thread and self.worker_thread.isRunning():
            self.worker_thread.quit(); self.worker_thread.wait(5000)
        event.accept()


def run() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("AutoClip Studio")
    app.setOrganizationName("AutoClip")
    window = MainWindow(); window.show()
    return app.exec()
