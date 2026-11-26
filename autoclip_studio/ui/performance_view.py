from __future__ import annotations

import sys
from dataclasses import asdict
from typing import Callable

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QFrame, QGridLayout, QGroupBox,
    QHBoxLayout, QLabel, QPushButton, QSizePolicy, QSpinBox, QVBoxLayout, QWidget,
)

from ..performance import PROFILES, HardwareMonitor, PerformanceSettings, resolve_profile_limits
from .style import SectionCard, configure_form_layout, configure_input


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

        profile_group = SectionCard("Performance profile")
        profile_form = configure_form_layout(QFormLayout())
        profile_group.body_layout().addLayout(profile_form)
        self.profile_combo = configure_input(QComboBox())
        self.profile_combo.addItems(["eco", "balanced", "turbo"])
        self.profile_combo.setCurrentText(settings.profile if settings.profile in PROFILES else "balanced")
        self.profile_desc = QLabel(PROFILES[self.profile_combo.currentText()], objectName="muted")
        self.profile_desc.setWordWrap(True)
        self.profile_desc.setMinimumWidth(0)
        self.profile_desc.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self.limits_label = QLabel("", objectName="muted")
        self.limits_label.setWordWrap(True)
        self.limits_label.setMinimumWidth(0)
        self.limits_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self.profile_combo.currentTextChanged.connect(self._profile_changed)
        profile_form.addRow("Active profile", self.profile_combo)
        profile_form.addRow("Behavior", self.profile_desc)
        profile_form.addRow("Resolved limits", self.limits_label)
        layout.addWidget(profile_group)

        guard_group = SectionCard("Thermal protection & Gaming Mode")
        guard_form = configure_form_layout(QFormLayout())
        guard_group.body_layout().addLayout(guard_form)
        self.thermal_check = QCheckBox("Pause between stages if GPU temperature exceeds threshold")
        self.thermal_check.setChecked(settings.thermal_protection)
        self.temp_limit = configure_input(QSpinBox())
        self.temp_limit.setRange(70, 92)
        self.temp_limit.setValue(settings.gpu_temp_limit_c)
        self.temp_limit.setSuffix(" °C")
        self.temp_resume = configure_input(QSpinBox())
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
            ("ram", "Unified Memory / RAM" if sys.platform == "darwin" else "System RAM"),
            ("gpu", "GPU"),
            ("vram", "Memory Pressure / Swap" if sys.platform == "darwin" else "VRAM (System / App)"),
            ("gpu_temp", "Thermal / Power" if sys.platform == "darwin" else "GPU Temp / Power"),
            ("cpu_temp", "CPU Temp"),
            ("power", "Power / Battery"),
            ("accel", "Acceleration"),
            ("gaming", "Gaming Mode"),
        ]
        for idx, (key, title) in enumerate(fields):
            row, col = divmod(idx, 2)
            grid.addWidget(QLabel(title, objectName="muted"), row, col * 2)
            val = QLabel("N/A")
            val.setWordWrap(True)
            val.setMinimumWidth(0)
            self._metric_values[key] = val
            grid.addWidget(val, row, col * 2 + 1)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
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
        core_detail = (
            f"{snap.cpu_perf_cores}P+{snap.cpu_eff_cores}E"
            if snap.cpu_perf_cores is not None and snap.cpu_eff_cores is not None
            else f"{snap.cpu_physical}C/{snap.cpu_logical}T"
        )
        rosetta_warn = " [Rosetta x86_64!]" if snap.rosetta_active else ""
        self._metric_values["cpu"].setText(
            f"{snap.cpu_percent:.0f}% ({core_detail}){rosetta_warn}"
            if snap.cpu_percent is not None else "N/A"
        )
        mem_suffix = " (Unified)" if snap.unified_memory else ""
        self._metric_values["ram"].setText(
            f"{snap.ram_used_mib / 1024:.1f} / {snap.ram_total_mib / 1024:.1f} GB{mem_suffix}"
            if snap.ram_used_mib is not None and snap.ram_total_mib is not None else "N/A"
        )
        if snap.gpu_name:
            util = f" ({snap.gpu_util_percent}%)" if snap.gpu_util_percent is not None else ""
            self._metric_values["gpu"].setText(f"{snap.gpu_name}{util}")
        else:
            self._metric_values["gpu"].setText("N/A")
        if snap.unified_memory:
            mp = snap.memory_pressure or "N/A"
            sw = f" • Swap {snap.swap_used_mib / 1024:.1f} GB" if snap.swap_used_mib is not None else ""
            self._metric_values["vram"].setText(f"{mp}{sw}")
        else:
            if snap.vram_used_mib is not None and snap.vram_total_mib is not None:
                app_txt = (
                    f"App {snap.vram_app_mib:,} MiB"
                    if snap.vram_app_mib is not None
                    else ("App Active (WDDM)" if snap.vram_app_active else "App 0 MiB (Idle)")
                )
                self._metric_values["vram"].setText(
                    f"System {snap.vram_used_mib:,} / {snap.vram_total_mib:,} MiB  •  {app_txt}"
                )
            else:
                self._metric_values["vram"].setText("N/A")
        if snap.unified_memory:
            self._metric_values["gpu_temp"].setText(snap.thermal_pressure or "N/A")
        else:
            temp_text = f"{snap.gpu_temp_c} °C" if snap.gpu_temp_c is not None else "N/A"
            power_text = f"{snap.gpu_power_w:.1f} W" if snap.gpu_power_w is not None else "N/A"
            self._metric_values["gpu_temp"].setText(f"{temp_text} / {power_text}")
        self._metric_values["cpu_temp"].setText(f"{snap.cpu_temp_c:.0f} °C" if snap.cpu_temp_c is not None else "N/A")
        if snap.power_source or snap.battery_percent is not None:
            src = snap.power_source or "Power"
            batt = f" • {snap.battery_percent}%" if snap.battery_percent is not None else ""
            chg = " (Charging)" if snap.battery_charging is True else (" (Discharging)" if snap.battery_charging is False else "")
            self._metric_values["power"].setText(f"{src}{batt}{chg}")
        else:
            self._metric_values["power"].setText("N/A")
        self._metric_values["accel"].setText(", ".join(snap.accel_backends) if snap.accel_backends else "CPU")
        if not settings.gaming_mode:
            self._metric_values["gaming"].setText("Off")
        else:
            self._metric_values["gaming"].setText(
                f"Paused ({settings.gaming_process} active)" if snap.gaming_active else f"Armed ({settings.gaming_process} idle)"
            )
