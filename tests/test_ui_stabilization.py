"""Regression tests for Global UI Stabilization: shared components, unified sizing, New Project, Queue, and Clips."""
from __future__ import annotations

import os
from dataclasses import asdict
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QFormLayout, QLabel

from autoclip_studio.config import SettingsStore
from autoclip_studio.database import Database
from autoclip_studio.gui import MainWindow
from autoclip_studio.media import DownloadProgress
from autoclip_studio.models import ProjectOptions
from autoclip_studio.transcription import SpeechProgress
from autoclip_studio.ui.components import MetricGrid, StageProgressHeader, StatusBadge, make_button
from autoclip_studio.ui.queue_workspace import compute_pipeline_stages
from autoclip_studio.ui.theme import theme_manager


def _app() -> QApplication:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QApplication.instance() or QApplication([])


def test_shared_components_variants_badges_and_responsive_metric_grid() -> None:
    _app()
    for variant in ("primary", "secondary", "ghost", "destructive", "subtle"):
        btn = make_button("Action", variant=variant, icon_name="check")  # type: ignore[arg-type]
        assert btn.text() == "Action"
        assert not btn.icon().isNull()

    badge = StatusBadge("Pending", status="pending")
    assert badge.property("tone") == "warning"
    badge.set_status("Ready", "ready")
    assert badge.property("tone") == "success"
    badge.set_status("Failed", "failed")
    assert badge.property("tone") == "danger"

    grid = MetricGrid(
        (("a", "Rate"), ("b", "Downloaded"), ("c", "Total"), ("d", "ETA"), ("e", "Memory")),
        full_width_keys=("e",),
    )
    app = _app()
    grid.show()
    grid.resize(800, 120)
    app.processEvents()
    assert grid._columns == 4
    grid.resize(420, 180)
    app.processEvents()
    assert grid._columns == 2
    grid.close()
    grid.set_value("a", "18.5 MB/s")
    assert grid.values["a"].text() == "18.5 MB/s"

    hdr = StageProgressHeader("Downloading video")
    hdr.set_indeterminate(True)
    assert hdr.bar.maximum() == 0
    hdr.set_indeterminate(False)
    assert hdr.bar.maximum() == 1000


def test_pipeline_stepper_keeps_download_active_when_message_indicates_downloading() -> None:
    stages = compute_pipeline_stages(
        {"source": "https://www.youtube.com/watch?v=dSq0Z5XpoLc", "face_tracking": True, "captions": True},
        status="processing",
        progress=12,
        stage_message="Downloading source video",
    )
    assert stages[0]["key"] == "ingest"
    assert stages[0]["state"] == "running"
    assert stages[1]["state"] == "pending"


def test_unified_controls_active_job_single_cancel_and_small_window_stability(tmp_path: Path, monkeypatch) -> None:
    app = _app()
    db = Database(tmp_path / "ui.db")
    settings = SettingsStore(tmp_path / "settings.json")
    monkeypatch.setattr(settings, "api_key", lambda *_a: "")
    monkeypatch.setattr(settings, "set_api_key", lambda *_a: None)
    monkeypatch.setattr("autoclip_studio.gui.Database", lambda: db)
    monkeypatch.setattr("autoclip_studio.gui.SettingsStore", lambda: settings)

    clip_file = tmp_path / "clip.mp4"
    clip_file.write_bytes(b"mp4")
    pid = db.create_project(
        "https://www.youtube.com/watch?v=dSq0Z5XpoLc",
        asdict(ProjectOptions("https://www.youtube.com/watch?v=dSq0Z5XpoLc", project_name="Podcast")),
    )
    db.update_project(pid, status="completed", progress=100)
    db.add_clip(
        pid, str(clip_file),
        "Asal-Usul Nama Gia Pratama dan Tirta yang Panjang dan Natural Membungkus Beberapa Baris",
        "Hook", "Deskripsi panjang yang tetap terbaca rapi tanpa scroll horizontal.", 91, 34.3,
    )

    win = MainWindow()
    try:
        win.resize(1280, 860)
        win.show()
        win.show_page(win.PAGE_PROJECT)
        app.processEvents()

        # Unified control heights across LineEdit, ComboBox, SpinBox, and Buttons (within 2px tolerance)
        heights = {
            win.source_input.height(),
            win.reframe_combo.height(),
            win.reframe_style_combo.height(),
            win.generate_button.height(),
            win.youtube_tab_btn.height(),
        }
        assert max(heights) - min(heights) <= 2
        assert abs(win.clip_count.height() - win.aspect.height()) <= 2

        # Automation card uses cohesive QFormLayout (not 50/50 stretched label grid)
        assert isinstance(win.reframe_combo.parentWidget().layout().itemAt(1).layout(), QFormLayout) or any(
            isinstance(ch, QFormLayout) for ch in win.reframe_combo.parentWidget().findChildren(QFormLayout)
        )

        # Processing Queue: single contextual Cancel CTA (no duplicate visible inner cancel button!)
        win.show_page(win.PAGE_QUEUE)
        win.active_job_card.start_job(
            {"id": pid, "source": "https://www.youtube.com/watch?v=dSq0Z5XpoLc", "options": {"project_name": "Podcast"}},
            options_dict={"source": "https://www.youtube.com/watch?v=dSq0Z5XpoLc"},
            profile_name="balanced",
            initial_progress=12,
            status_message="Downloading source video",
        )
        win.show_download(True)
        win.on_download_progress(DownloadProgress(
            stream="video", downloaded=100_000_000, total=500_000_000,
            bytes_per_second=12_000_000.0, eta_seconds=33.0, estimated=False, finished=False,
        ))
        app.processEvents()
        assert win.cancel_button.text() == "Cancel Download"
        assert not win.download_view.cancel_button.isVisible()

        win.show_download(False)
        win.show_speech(True)
        win.on_speech_progress(SpeechProgress(
            stage="Transcribing audio", engine="MLX Whisper", model="small", device="mlx", compute="float16",
            elapsed=12.0, processed=45.0, duration=300.0, eta=60.0, speed=4.0, rtf=0.25, reliable_percentage=True,
        ))
        app.processEvents()
        assert win.cancel_button.text() == "Cancel Transcription"
        assert not win.speech_progress.cancel_button.isVisible()

        # Small window (920x680) preserves stepper & stage_box readability without collapse!
        win.resize(920, 680)
        app.processEvents()
        assert win.active_job_card.stepper.height() >= 44
        assert win.active_job_card.stage_box.height() >= 90

        # Generated Clips at small window (920x680) has zero horizontal scroll & non-overlapping transport
        win.show_page(win.PAGE_CLIPS)
        theme_manager().apply("light")
        app.processEvents()
        assert win.clips_library.scroll.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        vol_right = win.clip_detail.volume_slider.geometry().right()
        fs_left = win.clip_detail.fullscreen_btn.geometry().left()
        assert vol_right < fs_left
    finally:
        theme_manager().apply("dark")
        win.close()
        app.processEvents()
        win.deleteLater()
