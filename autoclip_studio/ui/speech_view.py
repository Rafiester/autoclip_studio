from __future__ import annotations

import re
from dataclasses import asdict
import os
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QElapsedTimer, QTimer, Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QFormLayout, QGridLayout, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QProgressBar, QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from ..config import SettingsStore
from ..paths import CACHE_DIR
from ..speech_engine import (
    MODELS, XXL_RELEASES, SpeechSettings, download_speech_model, find_executable, inspect_speech,
    install_standalone, model_cache_root,
)
from ..transcription import SpeechProgress
from .download_view import cancel_icon
from .style import configure_form_layout, configure_input


def _clock(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    hours, remaining = divmod(int(seconds), 3600)
    minutes, seconds = divmod(remaining, 60)
    return f"{hours}:{minutes:02}:{seconds:02}" if hours else f"{minutes:02}:{seconds:02}"


class SpeechProgressView(QWidget):
    def __init__(self) -> None:
        super().__init__(objectName="downloadView")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        header = QHBoxLayout()
        self.stage = QLabel("Preparing transcription…", objectName="transferStatus")
        self.stage.setWordWrap(True)
        self.cancel_button = QPushButton(cancel_icon(), "Cancel Transcription", objectName="cancelDownload")
        header.addWidget(self.stage, 1)
        header.addWidget(self.cancel_button)
        layout.addLayout(header)
        self.engine = QLabel(objectName="transferSummary")
        self.engine.setWordWrap(True)
        self.engine.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.engine)
        bar_layout = QHBoxLayout()
        self.bar = QProgressBar(objectName="downloadTransfer")
        self.bar.setFixedHeight(9)
        self.bar.setAccessibleName("Transcription progress")
        self.bar.setTextVisible(False)
        self.percent = QLabel("—", objectName="transferMetric")
        bar_layout.addWidget(self.bar, 1)
        bar_layout.addWidget(self.percent)
        layout.addLayout(bar_layout)
        grid = QGridLayout()
        self.values = {}
        for i, name in enumerate(("Audio through", "Elapsed", "Time left", "Speed / RTF", "GPU / VRAM")):
            row, col = divmod(i, 2)
            label = QLabel(name, objectName="transferMetricLabel")
            value = QLabel("—", objectName="transferMetric")
            value.setWordWrap(True)
            grid.addWidget(label, row, col * 2)
            grid.addWidget(value, row, col * 2 + 1)
            self.values[name] = value
        layout.addLayout(grid)
        self.clock = QElapsedTimer()
        self.elapsed = 0.0
        self.timer = QTimer(self, interval=500)
        self.timer.timeout.connect(self._tick)

    def begin(self) -> None:
        self.stage.setText("Preparing transcription…")
        self.engine.clear()
        self.bar.setRange(0, 1000)
        self.bar.setValue(0)
        self.percent.setText("—")
        for value in self.values.values():
            value.setText("—")
        self.cancel_button.setEnabled(True)
        self.elapsed = 0.0
        self.clock.start()
        self.timer.start()

    def update_transcription(self, progress: SpeechProgress) -> None:
        self.stage.setText(progress.stage)
        self.engine.setText(" · ".join(value for value in
                             (progress.engine, progress.model, progress.device.upper(), progress.compute) if value))
        if progress.processed is not None and progress.duration and progress.reliable_percentage:
            percent = min(100, 100 * progress.processed / progress.duration)
            self.percent.setText(f"{percent:.1f}%")
            self.bar.setValue(round(percent * 10))
        else:
            self.percent.setText("—")
            self.bar.setValue(0)
        self.bar.setAccessibleDescription("Percentage unavailable" if self.percent.text() == "—" else self.percent.text())
        self.elapsed = progress.elapsed
        self.clock.restart()
        self._tick()
        self.values["Audio through"].setText(f"{_clock(progress.processed)} / {_clock(progress.duration)}")
        self.values["Time left"].setText(_clock(progress.eta))
        self.values["Speed / RTF"].setText(f"{progress.speed:.1f}× / {1 / progress.speed:.2f}"
                                           if progress.speed and progress.speed > 0 else "—")
        gpu = progress.gpu
        utilization = f"{gpu.utilization}% · " if gpu and gpu.utilization is not None else ""
        self.values["GPU / VRAM"].setText(f"{utilization}{gpu.used_mib:,}/{gpu.total_mib:,} MiB" if gpu else "—")

    def _tick(self) -> None:
        self.values["Elapsed"].setText(_clock(self.elapsed + self.clock.elapsed() / 1000))


class SpeechSettingsView(QWidget):
    def __init__(self, settings: SettingsStore, update_project: Callable[[], None],
                 start_task: Callable[[Callable, Callable], bool]) -> None:
        super().__init__()
        self.settings = settings
        self.update_project = update_project
        self.start_task = start_task
        current = settings.speech()
        form = configure_form_layout(QFormLayout(self))
        self.engine = configure_input(QComboBox())
        engines = [("Auto", "auto"), ("Purfview Faster-Whisper-XXL", "purfview"),
                   ("Python Faster-Whisper", "python"), ("MLX Whisper (Apple Silicon)", "mlx")]
        for name, value in engines:
            self.engine.addItem(name, value)
        self.engine.setCurrentIndex(max(0, self.engine.findData(current.engine)))
        self.model = self._combo(MODELS, settings.data["whisper_model"])
        self.device = self._combo(("auto", "cuda", "cpu"), settings.data["whisper_device"])
        self.compute = self._combo(("auto", "float16", "int8_float16", "int8", "float32"), current.compute_type)
        self.batch = configure_input(QSpinBox())
        self.batch.setRange(0, 16)
        self.batch.setSpecialValueText("Auto")
        self.batch.setToolTip("Auto uses validated sequential inference. Manual batch sizes are capped by free VRAM.")
        self.batch.setValue(current.batch_size)
        self.language = configure_input(QLineEdit(current.language))
        self.language.setPlaceholderText("Auto when empty, or a manual code such as en or id")
        self.vad = QCheckBox("Filter non-speech with VAD")
        self.vad.setChecked(current.vad)
        self.words = QCheckBox("Generate word-level timestamps")
        self.words.setChecked(current.word_timestamps)
        self.fallback = QCheckBox("Allow CPU fallback")
        self.fallback.setChecked(current.cpu_fallback)
        self.srt = QCheckBox("Export transcript JSON and SRT beside the clips")
        self.srt.setChecked(current.export_srt)
        for name, widget in (("Engine", self.engine), ("Model", self.model), ("Device", self.device),
                             ("Compute type", self.compute), ("Batch size", self.batch), ("Language", self.language)):
            form.addRow(name, widget)
        self.exe_path = self._path(form, "Executable", current.executable, directory=False)
        self.model_path = self._path(form, "Model directory", current.model_directory, directory=True)
        detected = find_executable(current.executable)
        if detected:
            self.exe_path.setPlaceholderText(str(detected))
        for checkbox in (self.vad, self.words, self.fallback, self.srt):
            form.addRow(checkbox)
        actions = QGridLayout()
        actions.setHorizontalSpacing(10)
        actions.setVerticalSpacing(8)
        actions.setColumnStretch(0, 1)
        actions.setColumnStretch(1, 1)
        for index, (name, callback) in enumerate((
            ("Save Speech Settings", self.save), ("Test Engine", self.test),
            ("Download Model", self.download), ("Open Model Cache", self.open_model_cache),
            ("Get Standalone Engine", self.get_engine), ("Import Standalone Folder", self.import_engine),
            ("Open Transcript Cache", self.open_transcript_cache), ("Clear Transcript Cache", self.clear_cache),
        )):
            button = QPushButton(name, objectName="primary" if index == 0 else "")
            button.clicked.connect(callback)
            actions.addWidget(button, index // 2, index % 2)
        form.addRow(actions)
        default_status = (
            "Purfview detected. Test the engine to check CUDA and local models." if detected
            else ("MLX Whisper (Apple Silicon) and Python Faster-Whisper are available." if os.name != "nt"
                  else "Python Faster-Whisper is available. A separate Purfview installation is optional.")
        )
        self.status = QLabel(default_status)
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        form.addRow(self.status)

    @staticmethod
    def _combo(items: tuple[str, ...], current: str) -> QComboBox:
        field = configure_input(QComboBox())
        field.addItems(items)
        field.setCurrentText(current)
        return field

    def _path(self, layout: QFormLayout, label: str, value: str, *, directory: bool) -> QLineEdit:
        field = configure_input(QLineEdit(value))
        field.setAccessibleName(label)
        browse = QPushButton("Browse")

        def choose() -> None:
            exe_filter = "Windows executable (*.exe)" if os.name == "nt" else "All files (*)"
            path = (QFileDialog.getExistingDirectory(self, label, field.text()) if directory else
                    QFileDialog.getOpenFileName(self, label, field.text(), exe_filter)[0])
            if path:
                field.setText(path)

        browse.clicked.connect(choose)
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(field, 1)
        row.addWidget(browse)
        layout.addRow(label, row)
        return field

    def _values(self) -> SpeechSettings:
        language = self.language.text().strip().lower()
        if language and re.fullmatch(r"[a-z]{2,3}", language) is None:
            raise ValueError("Enter a Whisper language code such as en or id, or leave Language empty for Auto.")
        return SpeechSettings(
            engine=self.engine.currentData(), compute_type=self.compute.currentText(), batch_size=self.batch.value(),
            language=language, vad=self.vad.isChecked(), word_timestamps=self.words.isChecked(),
            cpu_fallback=self.fallback.isChecked(), export_srt=self.srt.isChecked(),
            model_directory=self.model_path.text().strip(), executable=self.exe_path.text().strip(),
        )

    def save(self) -> None:
        try:
            values = self._values()
            self.settings.data.update(speech=asdict(values), whisper_model=self.model.currentText(),
                                      whisper_device=self.device.currentText())
            self.settings.save()
        except (OSError, ValueError) as error:
            self.status.setText(str(error))
            return
        self.update_project()
        self.status.setText("Speech-to-Text settings saved.")

    def _start(self, work: Callable, done: Callable, label: str) -> None:
        if self.start_task(work, done):
            self.status.setText(label)

    def test(self) -> None:
        try:
            values = self._values()
        except ValueError as error:
            self.status.setText(str(error))
            return
        model = self.model.currentText()
        self._start(lambda: inspect_speech(values, model), self.status.setText, "Checking engine, models and GPU…")

    def download(self) -> None:
        try:
            values = self._values()
        except ValueError as error:
            self.status.setText(str(error))
            return
        model = self.model.currentText()
        self._start(lambda: download_speech_model(model, values),
                    lambda path: self.status.setText(f"{model} is ready at {path}"), f"Preparing {model} weights…")

    def get_engine(self) -> None:
        QDesktopServices.openUrl(QUrl(XXL_RELEASES))

    def import_engine(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Import the extracted standalone Purfview folder")
        if not folder:
            return
        source = Path(folder)

        def done(executable: Path) -> None:
            self.exe_path.setText(str(executable))
            if not self.model_path.text() and (source / "_models").is_dir():
                self.model_path.setText(str(source / "_models"))
            self.status.setText("Standalone engine imported. Save Speech Settings to use it for projects.")

        self._start(lambda: install_standalone(source), done, "Importing standalone engine…")

    def open_model_cache(self) -> None:
        self._open(model_cache_root())

    def open_transcript_cache(self) -> None:
        self._open(CACHE_DIR / "transcripts")

    def clear_cache(self) -> None:
        answer = QMessageBox.question(
            self, "Clear Transcript Cache", "Remove reusable transcript cache files? Failed-project checkpoints and exports are kept.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        def clear() -> int:
            paths = [p for p in (CACHE_DIR / "transcripts").glob("*.json")
                     if re.fullmatch(r"[a-f0-9]{20}|[a-f0-9]{32}", p.stem)]
            for path in paths:
                path.unlink(missing_ok=True)
            return len(paths)

        self._start(clear, lambda count: self.status.setText(f"Removed {count} cached transcripts."), "Clearing cache…")

    def _open(self, path: Path) -> None:
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            self.status.setText(str(error))
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.resolve())))
