from __future__ import annotations

import os
import threading
from pathlib import Path

from PySide6.QtWidgets import QApplication

from autoclip_studio.config import SettingsStore
from autoclip_studio.highlights import select_highlights
from autoclip_studio.models import Segment, Transcript, Word
from autoclip_studio.performance import (
    HardwareMonitor, HardwareSnapshot, PerformanceSettings, resolve_profile_limits,
)
from autoclip_studio.process import ProcessManager
from autoclip_studio.tracking import track_faces
from autoclip_studio.ui.performance_view import PerformanceView


def _app() -> QApplication:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QApplication.instance() or QApplication([])


def test_profile_limits_scale_appropriately_for_rtx3050_6gb() -> None:
    snap = HardwareSnapshot(
        cpu_percent=25.0,
        cpu_physical=6,
        cpu_logical=12,
        ram_total_mib=15680,
        ram_used_mib=9000,
        ram_available_mib=6680,
        gpu_name="NVIDIA GeForce RTX 3050 6GB Laptop GPU",
        gpu_util_percent=10,
        vram_total_mib=6144,
        vram_used_mib=512,
        vram_free_mib=5632,
        gpu_temp_c=48,
        gpu_power_w=15.0,
        cpu_temp_c=None,
    )
    eco = resolve_profile_limits("eco", snap)
    bal = resolve_profile_limits("balanced", snap)
    turbo = resolve_profile_limits("turbo", snap)

    assert eco.cpu_threads < bal.cpu_threads <= turbo.cpu_threads
    assert eco.whisper_batch_size <= bal.whisper_batch_size <= turbo.whisper_batch_size
    assert eco.tracking_sample_interval > bal.tracking_sample_interval > turbo.tracking_sample_interval
    assert eco.nvenc_preset == "p6"
    assert bal.nvenc_preset == "p5"
    assert turbo.nvenc_preset == "p4"
    if os.name == "nt":
        assert eco.subprocess_priority_flags != 0
        assert bal.subprocess_priority_flags != eco.subprocess_priority_flags


def test_performance_settings_persistence(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    store = SettingsStore(path)
    assert store.performance().profile == "balanced"
    assert store.performance().gaming_mode is False

    store.data["performance"] = {
        "profile": "turbo",
        "thermal_protection": True,
        "gpu_temp_limit_c": 80,
        "gpu_temp_resume_c": 72,
        "gaming_mode": True,
        "gaming_process": "TslGame.exe",
    }
    store.save()

    reloaded = SettingsStore(path).performance()
    assert reloaded.profile == "turbo"
    assert reloaded.gpu_temp_limit_c == 80
    assert reloaded.gpu_temp_resume_c == 72
    assert reloaded.gaming_mode is True


def test_thermal_hysteresis_and_gaming_mode_wait_cancellation() -> None:
    monitor = HardwareMonitor()
    settings = PerformanceSettings(thermal_protection=True, gpu_temp_limit_c=83, gpu_temp_resume_c=75)

    assert not monitor.should_pause_for_thermal(settings, HardwareSnapshot(gpu_temp_c=80))
    assert monitor.should_pause_for_thermal(settings, HardwareSnapshot(gpu_temp_c=83))
    # Hysteresis: stays paused at 78°C until <= 75°C
    assert monitor.should_pause_for_thermal(settings, HardwareSnapshot(gpu_temp_c=78))
    assert not monitor.should_pause_for_thermal(settings, HardwareSnapshot(gpu_temp_c=75))
    # Missing sensor never triggers false pause
    assert not monitor.should_pause_for_thermal(settings, HardwareSnapshot(gpu_temp_c=None))

    cancel = threading.Event()
    cancel.set()
    monitor.wait_if_constrained(settings, cancel, poll_seconds=0.01)
    monitor.shutdown()


def test_highlights_cache_hits_and_invalidates_on_count_change(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("autoclip_studio.highlights.CACHE_DIR", tmp_path / "cache")

    class FakeProvider:
        kind = "gemini"
        model = "gemini-2.5-flash"
        calls = 0

        def complete(self, _system: str, _prompt: str) -> str:
            FakeProvider.calls += 1
            return '{"highlights": [{"start": 0, "end": 30, "title": "Clip", "hook": "H", "description": "D", "score": 90, "reason": "R"}]}'

    words = [Word(0.0, 1.0, "hello"), Word(1.0, 2.0, "world")]
    transcript = Transcript(35.0, "en", [Segment(0.0, 32.0, "hello world", words)])
    provider = FakeProvider()

    first = select_highlights(transcript, 1, 15, 60, provider, lambda _m: None, cache_enabled=True)
    second = select_highlights(transcript, 1, 15, 60, provider, lambda _m: None, cache_enabled=True)
    assert len(first) == len(second) == 1
    assert FakeProvider.calls == 1

    # Changing count invalidates cache key
    select_highlights(transcript, 2, 15, 60, provider, lambda _m: None, cache_enabled=True)
    assert FakeProvider.calls == 2


def test_tracking_cache_reuses_points(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("autoclip_studio.tracking.CACHE_DIR", tmp_path / "cache")
    from autoclip_studio.process import find_media_tool

    source = tmp_path / "sample.mp4"
    manager = ProcessManager()
    manager.run([
        find_media_tool("ffmpeg"), "-hide_banner", "-y",
        "-f", "lavfi", "-i", "color=c=black:size=640x360:rate=15:duration=2",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", source,
    ])
    logs: list[str] = []
    p1 = track_faces(source, 0.0, 1.5, 200, manager, logs.append, sample_interval=0.5, cache_enabled=True)
    p2 = track_faces(source, 0.0, 1.5, 200, manager, logs.append, sample_interval=0.5, cache_enabled=True)
    assert p1 == p2
    assert any("Using cached face tracking" in line for line in logs)


def test_performance_view_shows_na_for_missing_sensors() -> None:
    _app()

    class StubMonitor(HardwareMonitor):
        def snapshot(self, gaming_process: str = "TslGame.exe") -> HardwareSnapshot:
            return HardwareSnapshot(cpu_physical=6, cpu_logical=12, cpu_temp_c=None)

    saved: list[PerformanceSettings] = []
    view = PerformanceView(StubMonitor(), PerformanceSettings(), saved.append)
    assert view._metric_values["cpu_temp"].text() == "N/A"
    assert view._metric_values["gpu"].text() == "N/A"
    view.save_button.click()
    assert len(saved) == 1
