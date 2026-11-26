from __future__ import annotations

import os
import sys
import time
from dataclasses import asdict
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from autoclip_studio.config import SettingsStore
from autoclip_studio.database import Database
from autoclip_studio.gui import MainWindow
from autoclip_studio.models import ProjectOptions
from autoclip_studio.transcription import (
    SpeechProgress,
    _InferenceMetricsTracker,
    sample_apple_memory_telemetry,
)
from autoclip_studio.ui.queue_workspace import JobCompletedDialog
from autoclip_studio.ui.speech_view import SpeechProgressView
from autoclip_studio.ui.theme import theme_manager


def _app() -> QApplication:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QApplication.instance() or QApplication([])


def test_inference_metrics_tracker_monotonic_speed_rtf_and_eta() -> None:
    tracker = _InferenceMetricsTracker(duration=180.0, smoothing_alpha=0.5)
    tracker.start_inference(180.0)
    # Simulate 2.0s elapsed active inference with 10.0s processed audio -> 5.0x realtime, RTF 0.20, ETA ~34s
    tracker.inference_started = time.perf_counter() - 2.0
    processed, speed, rtf, eta, inf_elapsed = tracker.update(10.0)
    assert processed == pytest.approx(10.0, abs=0.05)
    assert speed is not None and 4.5 <= speed <= 5.5
    assert rtf is not None and 0.18 <= rtf <= 0.22
    assert eta is not None and 30.0 <= eta <= 38.0
    assert inf_elapsed is not None and inf_elapsed >= 1.9

    # Non-monotonic dip must be clamped monotonic; values beyond duration clamped to duration
    p2, *_ = tracker.update(4.0)
    assert p2 == pytest.approx(10.0, abs=0.05)
    p3, *_ = tracker.update(999.0)
    assert p3 == pytest.approx(180.0, abs=0.05)


def test_apple_silicon_memory_telemetry_and_speech_view_modes() -> None:
    app = _app()
    view = SpeechProgressView()
    view.begin()
    assert view.bar.maximum() == 0  # Indeterminate before measurable progress

    if sys.platform == "darwin":
        tel = sample_apple_memory_telemetry("balanced")
        assert tel is not None
        assert "unified_total_gb" in tel and "app_mib" in tel
    else:
        tel = {"mlx_active_mib": 320.0, "mlx_peak_mib": 480.0, "app_mib": 610.0, "unified_used_gb": 5.2, "unified_total_gb": 8.0}

    view.update_transcription(SpeechProgress(
        stage="Transcribing audio",
        engine="MLX Whisper (Apple Silicon)",
        model="small",
        device="mlx",
        compute="float16",
        elapsed=40.0,
        processed=200.0,
        duration=1800.0,
        eta=320.0,
        speed=5.0,
        rtf=0.20,
        apple_telemetry=tel,
        reliable_percentage=True,
    ))
    assert view.bar.maximum() == 1000
    assert view.percent.text() == "11.1%"
    assert "200s / 1800s" in view.stage.text()
    assert view.values["Audio through"].text() == "03:20 / 30:00"
    assert view.values["Speed / RTF"].text() == "5.0× / 0.20"
    assert view.values["Time left"].text() == "05:20"
    assert view.labels["GPU / VRAM"].text() == "Apple Silicon Memory"
    assert "VRAM" not in view.values["GPU / VRAM"].text()
    view.deleteLater()
    app.processEvents()


def test_queue_recent_activity_sorting_contextual_ctas_and_completion_popup(tmp_path: Path, monkeypatch) -> None:
    app = _app()
    db = Database(tmp_path / "queue_ui.db")
    settings = SettingsStore(tmp_path / "settings.json")
    monkeypatch.setattr(settings, "api_key", lambda *_a: "")
    monkeypatch.setattr(settings, "set_api_key", lambda *_a: None)
    monkeypatch.setattr("autoclip_studio.gui.Database", lambda: db)
    monkeypatch.setattr("autoclip_studio.gui.SettingsStore", lambda: settings)

    # Create an older failed job, then a newer completed job with a clip
    clip_file = tmp_path / "clip1.mp4"
    clip_file.write_bytes(b"mp4data")
    p_old_fail = db.create_project("https://www.youtube.com/watch?v=11111111111", asdict(ProjectOptions("https://www.youtube.com/watch?v=11111111111", project_name="Old Failed Job")))
    db.update_project(p_old_fail, status="analysis_failed", progress=40, error="Quota reached")

    time.sleep(0.05)
    p_new_done = db.create_project("https://www.youtube.com/watch?v=22222222222", asdict(ProjectOptions("https://www.youtube.com/watch?v=22222222222", project_name="Newly Completed Job")))
    db.update_project(p_new_done, status="completed", progress=100)
    db.add_clip(p_new_done, str(clip_file), "Awesome Highlight Title That Wraps Nicely", "Hook", "Complete description text here", 92, 42.5, ai_score=90, replay_score=95, reason="Peak replay moment", detection_mode="hybrid")

    win = MainWindow()
    try:
        win.show()
        win.show_page(2)
        app.processEvents()
        # No historical completion popup on startup!
        assert win._active_completion_dialog is None

        # Newly completed job MUST appear ABOVE older failed job under default Recent Activity!
        ordered_ids = [str(p["id"]) for p in win.queue_panel._visible_projects]
        assert ordered_ids.index(p_new_done) < ordered_ids.index(p_old_fail)

        # Contextual primary CTAs exist on both cards
        done_card = win.queue_panel._cards[p_new_done]
        fail_card = win.queue_panel._cards[p_old_fail]
        assert done_card.primary_action_btn is not None and "View Generated Clips" in done_card.primary_action_btn.text()
        assert fail_card.primary_action_btn is not None and "Retry AI Analysis" in fail_card.primary_action_btn.text()

        # Trigger completion popup once (and verify deduplication on second call)
        win.on_completed(p_new_done, 1)
        app.processEvents()
        dlg = win._active_completion_dialog
        assert isinstance(dlg, JobCompletedDialog)
        win.on_completed(p_new_done, 1)
        assert win._active_completion_dialog is dlg  # Not duplicated!

        # Primary CTA navigates to Generated Clips and filters to the completed project
        dlg.view_btn.click()
        app.processEvents()
        assert win.stack.currentIndex() == 3
        assert win.clips_library.project_filter_bar.isVisible()
        assert win.clips_library.card_count() == 1

        # Verify Generated Clips library has horizontal scroll disabled and Light theme updates cleanly
        assert win.clips_library.scroll.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        theme_manager().apply("light")
        app.processEvents()
        assert not win.clip_detail.play_btn.icon().isNull()

        # Verify fullscreen toggles & exits cleanly without losing player output
        win.clip_detail.toggle_fullscreen()
        app.processEvents()
        assert win.clip_detail._fullscreen_win is not None
        win.clip_detail._exit_fullscreen()
        app.processEvents()
        assert win.clip_detail._fullscreen_win is None
    finally:
        theme_manager().apply("dark")
        win.close()
        app.processEvents()
        win.deleteLater()
