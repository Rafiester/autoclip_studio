from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from autoclip_studio import performance as perf
from autoclip_studio.config import SettingsManager, SettingsStore
from autoclip_studio.process import ProcessManager
from autoclip_studio.speech_engine import GpuInfo, Purfview, SpeechSettings, subtitle_edit_engine
from autoclip_studio.transcription import SpeechProgress, transcribe
from autoclip_studio.ui.performance_view import PerformanceView
from autoclip_studio.ui.speech_view import SpeechProgressView


def _app() -> QApplication:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QApplication.instance() or QApplication([])


def test_uncached_model_with_auto_engine_selects_purfview_cuda_not_cpu(monkeypatch, tmp_path: Path) -> None:
    """Regression test: missing cached model on clean/new size must NOT bypass Purfview CUDA for CPU."""
    media = tmp_path / "video.mp4"
    media.write_bytes(b"mp4")
    fake_exe = tmp_path / "faster-whisper-xxl.exe"
    fake_exe.write_bytes(b"exe")
    fake_model_dir = tmp_path / "models" / "faster-whisper-small"
    fake_model_dir.mkdir(parents=True)
    for fn in ("model.bin", "config.json", "tokenizer.json"):
        (fake_model_dir / fn).write_bytes(b"ok")

    monkeypatch.setattr("autoclip_studio.transcription.find_executable", lambda *_a: fake_exe)
    monkeypatch.setattr(
        "autoclip_studio.transcription.inspect_purfview",
        lambda _p: Purfview(fake_exe, "faster-whisper-xxl.exe 1.1.1", frozenset({"--batched", "--batch_size"}), True),
    )
    monkeypatch.setattr("autoclip_studio.transcription.gpu_info", lambda *_a: GpuInfo("RTX 3050", 6144, 5880, 264, 5))
    monkeypatch.setattr("autoclip_studio.transcription._python_cuda", lambda: False)
    monkeypatch.setattr("autoclip_studio.transcription._audio_timing", lambda *_a: (0.0, 12.0, 12.0))

    # Simulate uncached model initially -> find_model returns None until download_speech_model runs!
    downloaded = {"called": False}

    def fake_find_model(_name, _settings, _exe=None):
        return fake_model_dir if downloaded["called"] else None

    def fake_download(_name, _settings):
        downloaded["called"] = True
        return fake_model_dir

    monkeypatch.setattr("autoclip_studio.transcription.find_model", fake_find_model)
    monkeypatch.setattr("autoclip_studio.transcription.download_speech_model", fake_download)

    used_purfview: list[tuple[str, str, int]] = []

    def fake_run_purfview(_xxl, model_p, _mname, _media, device, compute, batch, *_a):
        used_purfview.append((device, compute, batch))
        return [{"start": 0.0, "end": 4.0, "text": "CUDA hello", "words": []}], "en", 12.0

    monkeypatch.setattr("autoclip_studio.transcription._run_purfview", fake_run_purfview)
    monkeypatch.setattr(
        "autoclip_studio.transcription._run_python",
        lambda *_a, **_kw: pytest.fail("Should NOT fall back to Python Faster-Whisper when Purfview CUDA is available"),
    )

    logs: list[str] = []
    progs: list[SpeechProgress] = []
    limits = perf.resolve_profile_limits(
        "balanced",
        perf.HardwareSnapshot(cpu_physical=6, cpu_logical=12, vram_total_mib=6144, vram_free_mib=5880),
    )
    tr = transcribe(
        media, "small", "auto", None, False, ProcessManager(), logs.append,
        settings=SpeechSettings(engine="auto", compute_type="auto"),
        progress=progs.append,
        profile_limits=limits,
    )
    assert downloaded["called"] is True
    assert used_purfview == [("cuda", "float16", 4)]
    assert tr.segments[0].text == "CUDA hello"
    assert any(p.engine == "Purfview Faster-Whisper-XXL" and p.device == "cuda" for p in progs)


def test_cpu_fallback_always_reports_reason_and_cpu_ram_telemetry(monkeypatch, tmp_path: Path) -> None:
    app = _app()
    media = tmp_path / "audio.mp4"
    media.write_bytes(b"mp4")

    monkeypatch.setattr("autoclip_studio.transcription.find_executable", lambda *_a: None)
    monkeypatch.setattr("autoclip_studio.transcription.find_model", lambda *_a: None)
    monkeypatch.setattr("autoclip_studio.transcription._python_cuda", lambda: False)
    monkeypatch.setattr("autoclip_studio.transcription.gpu_info", lambda *_a: GpuInfo("RTX 3050", 6144, 5880, 264, 0))
    monkeypatch.setattr("autoclip_studio.transcription._audio_timing", lambda *_a: (0.0, 5.0, 5.0))

    def fake_run_python(_m, _media, device, compute, _b, _lang, _s, _mgr, reporter, **_kw):
        reporter.emit("Transcribing audio", processed=2.5, duration=5.0, speed=3.0, reliable=True)
        return [{"start": 0.0, "end": 4.0, "text": "CPU text", "words": []}], "en", 5.0

    monkeypatch.setattr("autoclip_studio.transcription._run_python", fake_run_python)

    logs: list[str] = []
    progs: list[SpeechProgress] = []
    tr = transcribe(media, "small", "auto", None, False, ProcessManager(), logs.append, progress=progs.append)
    assert tr.segments[0].text == "CPU text"
    assert any("CUDA" in line or "Purfview" in line for line in logs)
    last_prog = progs[-1]
    assert last_prog.device == "cpu"
    assert last_prog.fallback_reason
    assert last_prog.cpu_percent is not None
    assert last_prog.ram_total_mib is not None

    view = SpeechProgressView()
    view.update_transcription(last_prog)
    assert view.labels["GPU / VRAM"].text() == "CPU / RAM"
    assert "CPU" in view.values["GPU / VRAM"].text()
    assert "RAM" in view.values["GPU / VRAM"].text()
    view.deleteLater()
    app.processEvents()


def test_speech_progress_and_performance_distinguish_system_and_app_vram(monkeypatch) -> None:
    app = _app()
    view = SpeechProgressView()
    gpu = GpuInfo(
        "NVIDIA GeForce RTX 3050 6GB Laptop GPU", 6144, 5300, 844, 42,
        app_used_mib=512, app_active=True, temperature_c=52, power_w=18.5, telemetry_source="nvml",
    )
    view.update_transcription(SpeechProgress(
        "Transcribing speech", "Purfview Faster-Whisper-XXL", "small", "cuda", "float16",
        10.0, processed=20.0, duration=40.0, speed=6.5, gpu=gpu, reliable_percentage=True,
    ))
    assert view.labels["GPU / VRAM"].text() == "GPU / VRAM"
    txt = view.values["GPU / VRAM"].text()
    assert "42%" in txt
    assert "System 844/6,144 MiB" in txt
    assert "App 512 MiB" in txt

    wddm_gpu = GpuInfo("RTX 3050", 6144, 5300, 844, 30, app_used_mib=None, app_active=True)
    view.update_transcription(SpeechProgress(
        "Transcribing speech", "Purfview Faster-Whisper-XXL", "small", "cuda", "float16",
        11.0, processed=22.0, duration=40.0, speed=6.5, gpu=wddm_gpu, reliable_percentage=True,
    ))
    assert "App Active (WDDM)" in view.values["GPU / VRAM"].text()
    view.deleteLater()

    class MockMonitor:
        def snapshot(self, _proc=""):
            return perf.HardwareSnapshot(
                cpu_percent=18.0, cpu_physical=6, cpu_logical=12,
                ram_total_mib=16384, ram_used_mib=8192, ram_available_mib=8192,
                gpu_name="NVIDIA GeForce RTX 3050", gpu_util_percent=35,
                vram_total_mib=6144, vram_used_mib=1024, vram_free_mib=5120,
                vram_app_mib=640, vram_app_active=True,
                gpu_temp_c=54, gpu_power_w=21.0,
                gpu_backend="nvidia", accel_backends=("CUDA (Purfview XXL)", "NVENC H.264"),
            )

    pv = PerformanceView(MockMonitor(), perf.PerformanceSettings(), lambda _s: None)
    assert "CUDA (Purfview XXL)" in pv._metric_values["accel"].text()
    if sys.platform != "darwin":
        assert "System 1,024 / 6,144 MiB" in pv._metric_values["vram"].text()
        assert "App 640 MiB" in pv._metric_values["vram"].text()
    pv.deleteLater()
    app.processEvents()


def test_settings_manager_persists_speech_and_profile_preferences(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    sm: SettingsStore = SettingsManager(path)
    sm.data["speech"]["engine"] = "purfview"
    sm.data["speech"]["compute_type"] = "float16"
    sm.data["whisper_device"] = "cuda"
    sm.data["performance"]["profile"] = "turbo"
    sm.save()

    reloaded = SettingsManager(path)
    assert reloaded.speech().engine == "purfview"
    assert reloaded.speech().compute_type == "float16"
    assert reloaded.data["whisper_device"] == "cuda"
    assert reloaded.performance().profile == "turbo"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows native Purfview CUDA check")
def test_windows_detect_acceleration_includes_cuda_and_nvenc_when_purfview_present() -> None:
    perf.detect_acceleration_backends.cache_clear()
    backends = perf.detect_acceleration_backends()
    exe = subtitle_edit_engine()
    if exe:
        assert any("CUDA" in b for b in backends)
