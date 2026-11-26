from __future__ import annotations

from dataclasses import asdict
from typing import Callable

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QFrame, QGridLayout, QGroupBox,
    QHBoxLayout, QLabel, QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from ..performance import PROFILES, HardwareMonitor, PerformanceSettings, resolve_profile_limits


class PerformanceView(QWidget):
    """Compact performance profile, thermal protection, Gaming Mode, and live telemetry panel."""

    def __init__(self, monitor: HardwareMonitor, settings: PerformanceSettings,
                 on_save: Callable[[PerformanceSettings], None]) -> None:
        super().__init__()
        self.monitor = monitor
        self._on_save = on_save
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        profile_group = QGroupBox("Performance profile")
        profile_form = QFormLayout(profile_group)
        profile_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.profile_combo = QComboBox()
        self.profile_combo.addItems(["eco", "balanced", "turbo"])
        self.profile_combo.setCurrentText(settings.profile if settings.profile in PROFILES else "balanced")
        self.profile_desc = QLabel(PROFILES[self.profile_combo.currentText()], objectName="muted")
        self.profile_desc.setWordWrap(True)
        self.limits_label = QLabel("", objectName="muted")
        self.limits_label.setWordWrap(True)
        self.profile_combo.currentTextChanged.connect(self._profile_changed)
        profile_form.addRow("Active profile", self.profile_combo)
        profile_form.addRow("Behavior", self.profile_desc)
        profile_form.addRow("Resolved limits", self.limits_label)
        layout.addWidget(profile_group)

        guard_group = QGroupBox("Thermal protection & Gaming Mode")
        guard_form = QFormLayout(guard_group)
        guard_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.thermal_check = QCheckBox("Pause between stages if GPU temperature exceeds threshold")
        self.thermal_check.setChecked(settings.thermal_protection)
        self.temp_limit = QSpinBox()
        self.temp_limit.setRange(70, 92)
        self.temp_limit.setValue(settings.gpu_temp_limit_c)
        self.temp_limit.setSuffix(" °C")
        self.temp_resume = QSpinBox()
        self.temp_resume.setRange(60, 85)
        self.temp_resume.setValue(settings.gpu_temp_resume_c)
        self.temp_resume.setSuffix(" °C")
        self.gaming_check = QCheckBox("Gaming Mode: defer heavy jobs while TslGame.exe is running")
        self.gaming_check.setChecked(settings.gaming_mode)
        guard_form.addRow("Thermal guard", self.thermal_check)
        guard_form.addRow("Pause threshold", self.temp_limit)
        guard_form.addRow("Resume threshold", self.temp_resume)
        guard_form.addRow("Gaming Mode", self.gaming_check)

        actions = QHBoxLayout()
        self.save_button = QPushButton("Save performance settings", objectName="primary")
        self.save_button.clicked.connect(self._save)
        self.status_label = QLabel("", objectName="muted")
        actions.addWidget(self.save_button)
        actions.addWidget(self.status_label, 1)
        guard_form.addRow(actions)
        layout.addWidget(guard_group)

        metrics_card = QFrame(objectName="card")
        metrics_layout = QVBoxLayout(metrics_card)
        metrics_layout.setContentsMargins(16, 14, 16, 14)
        metrics_layout.setSpacing(10)
        metrics_layout.addWidget(QLabel("Live hardware telemetry", objectName="sectionTitle"))
        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(8)
        self._metric_values: dict[str, QLabel] = {}
        fields = [
            ("cpu", "CPU"),
            ("ram", "RAM"),
            ("gpu", "GPU"),
            ("vram", "VRAM"),
            ("gpu_temp", "GPU Temp / Power"),
            ("cpu_temp", "CPU Temp"),
            ("gaming", "Gaming Mode"),
        ]
        for idx, (key, title) in enumerate(fields):
            row, col = divmod(idx, 2)
            grid.addWidget(QLabel(title, objectName="muted"), row, col * 2)
            val = QLabel("N/A")
            self._metric_values[key] = val
            grid.addWidget(val, row, col * 2 + 1)
        metrics_layout.addLayout(grid)
        layout.addWidget(metrics_card)
        layout.addStretch()

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self.refresh_metrics)
        self.refresh_metrics()

    def set_polling(self, enabled: bool) -> None:
        if enabled:
            self.refresh_metrics()
            if not self._timer.isActive():
                self._timer.start()
        elif self._timer.isActive():
            self._timer.stop()

    def current_settings(self) -> PerformanceSettings:
        limit = self.temp_limit.value()
        resume = min(limit - 2, self.temp_resume.value())
        return PerformanceSettings(
            profile=self.profile_combo.currentText(),
            thermal_protection=self.thermal_check.isChecked(),
            gpu_temp_limit_c=limit,
            gpu_temp_resume_c=resume,
            gaming_mode=self.gaming_check.isChecked(),
            gaming_process="TslGame.exe",
        )

    def _profile_changed(self, profile: str) -> None:
        self.profile_desc.setText(PROFILES.get(profile, ""))
        self.refresh_metrics()

    def _save(self) -> None:
        settings = self.current_settings()
        self._on_save(settings)
        self.status_label.setText("Performance settings saved (applies to next job or stage boundary).")

    def refresh_metrics(self) -> None:
        settings = self.current_settings()
        snap = self.monitor.snapshot(settings.gaming_process if settings.gaming_mode else "")
        limits = resolve_profile_limits(settings.profile, snap)
        hw_enc_label = "VideoToolbox" if snap.unified_memory else f"NVENC: {limits.nvenc_preset}"
        self.limits_label.setText(
            f"CPU threads: {limits.cpu_threads}  |  FFmpeg threads: {limits.ffmpeg_threads}  |  "
            f"Whisper batch: {limits.whisper_batch_size}  |  Tracking step: {limits.tracking_sample_interval:.2f}s  |  "
            f"{hw_enc_label}"
        )
        self._metric_values["cpu"].setText(
            f"{snap.cpu_percent:.0f}% ({snap.cpu_physical}C/{snap.cpu_logical}T)"
            if snap.cpu_percent is not None else "N/A"
        )
        mem_suffix = " (Unified)" if snap.unified_memory else ""
        self._metric_values["ram"].setText(
            f"{snap.ram_used_mib / 1024:.1f} / {snap.ram_total_mib / 1024:.1f} GB{mem_suffix}"
            if snap.ram_used_mib is not None and snap.ram_total_mib is not None else "N/A"
        )
        if snap.gpu_name:
            util = f"{snap.gpu_util_percent}%" if snap.gpu_util_percent is not None else "N/A"
            self._metric_values["gpu"].setText(f"{snap.gpu_name} ({util})")
        else:
            self._metric_values["gpu"].setText("N/A")
        if snap.unified_memory:
            self._metric_values["vram"].setText("Unified Memory")
        else:
            self._metric_values["vram"].setText(
                f"{snap.vram_used_mib} / {snap.vram_total_mib} MiB"
                if snap.vram_used_mib is not None and snap.vram_total_mib is not None else "N/A"
            )
        temp_text = f"{snap.gpu_temp_c} °C" if snap.gpu_temp_c is not None else "N/A"
        power_text = f"{snap.gpu_power_w:.1f} W" if snap.gpu_power_w is not None else "N/A"
        self._metric_values["gpu_temp"].setText(f"{temp_text} / {power_text}")
        self._metric_values["cpu_temp"].setText(f"{snap.cpu_temp_c:.0f} °C" if snap.cpu_temp_c is not None else "N/A")
        if not settings.gaming_mode:
            self._metric_values["gaming"].setText("Off")
        else:
            self._metric_values["gaming"].setText(
                f"Paused ({settings.gaming_process} active)" if snap.gaming_active else f"Armed ({settings.gaming_process} idle)"
            )
