import json
import os
import time
from dataclasses import asdict
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QLabel, QPushButton

from autoclip_studio.config import SettingsStore
from autoclip_studio.database import Database
from autoclip_studio.gui import MainWindow
from autoclip_studio.models import ProjectOptions
from autoclip_studio.ui.log_view import LogView
from autoclip_studio.ui.queue_workspace import (
    compute_pipeline_stages,
    estimate_eta_text,
    format_elapsed,
    project_display_name,
    project_source_info,
)


@pytest.fixture(scope="module")
def app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QApplication.instance() or QApplication([])


def test_pipeline_stepper_adapts_to_local_srt_disabled_steps_and_waiting():
    local_opts = {
        "source": r"C:\videos\demo.mp4",
        "srt_path": r"C:\videos\demo.srt",
        "face_tracking": False,
        "captions": False,
    }
    stages = compute_pipeline_stages(local_opts, "processing", 42, "Selecting highlights")
    by_key = {s["key"]: s for s in stages}
    assert by_key["ingest"]["title"] == "Local Import"
    assert by_key["ingest"]["state"] == "skipped"
    assert by_key["speech"]["title"] == "SRT Import"
    assert by_key["speech"]["state"] == "skipped"
    assert by_key["analysis"]["state"] == "running"
    assert by_key["tracking"]["state"] == "skipped"
    assert by_key["captions"]["state"] == "skipped"
    assert by_key["export"]["state"] == "pending"

    yt_opts = {
        "source": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "face_tracking": True,
        "captions": True,
    }
    waiting = compute_pipeline_stages(yt_opts, "processing", 25, "Waiting: gaming mode active")
    assert {s["key"]: s for s in waiting}["speech"]["state"] == "waiting"

    failed = compute_pipeline_stages(yt_opts, "analysis_failed", 40, "Quota exceeded")
    assert {s["key"]: s for s in failed}["analysis"]["state"] == "failed"

    done = compute_pipeline_stages(yt_opts, "completed", 100, "Done")
    assert all(s["state"] == "completed" for s in done)


def test_eta_only_estimates_when_measurable_progress_exists():
    now = time.monotonic()
    assert estimate_eta_text(now, 0) == "—"
    assert estimate_eta_text(now - 10.0, 2) == "—"
    assert estimate_eta_text(now - 1.0, 50) == "—"
    assert estimate_eta_text(now - 20.0, 50) == format_elapsed(20)


def test_database_queue_ordering_and_history_removal_preserves_media(tmp_path):
    db = Database(tmp_path / "queue.db")
    media_file = tmp_path / "source.mp4"
    clip_file = tmp_path / "clip.mp4"
    media_file.write_bytes(b"video")
    clip_file.write_bytes(b"clip")

    opts1 = asdict(ProjectOptions(str(media_file), project_name="First Job"))
    opts2 = asdict(ProjectOptions(str(media_file), project_name="Second Job"))
    opts3 = asdict(ProjectOptions(str(media_file), project_name="Third Job"))
    p1 = db.create_project(str(media_file), opts1)
    p2 = db.create_project(str(media_file), opts2)
    p3 = db.create_project(str(media_file), opts3)

    assert db.next_queued_project()["id"] == p1
    assert db.reorder_queued_project(p2, -1)
    assert db.next_queued_project()["id"] == p2
    assert db.reorder_queued_project(p3, -1)

    reloaded = Database(tmp_path / "queue.db")
    queued_ids = [
        r["id"]
        for r in sorted((r for r in reloaded.projects() if r["status"] == "queued"), key=lambda r: r["queue_order"])
    ]
    assert queued_ids == [p2, p3, p1]

    reloaded.add_clip(p1, str(clip_file), "Highlight 1", "Hook", "Desc", 9.0, 25.0)
    assert reloaded.delete_project_history(p1)
    assert all(r["id"] != p1 for r in reloaded.projects())
    assert media_file.exists() and clip_file.exists()


def _setup_window(monkeypatch, tmp_path, *, projects_seed=None):
    db = Database(tmp_path / "test.db")
    settings = SettingsStore(tmp_path / "settings.json")
    monkeypatch.setattr(settings, "api_key", lambda *_args: "")
    monkeypatch.setattr(settings, "set_api_key", lambda *_args: None)
    monkeypatch.setattr("autoclip_studio.gui.Database", lambda: db)
    monkeypatch.setattr("autoclip_studio.gui.SettingsStore", lambda: settings)
    if projects_seed:
        projects_seed(db)
    window = MainWindow()
    window.show()
    window.show_page(2)
    return window, db, settings


def test_queue_overview_active_card_filters_logs_and_responsive_layout(app, monkeypatch, tmp_path):
    media_file = tmp_path / "podcast.mp4"
    media_file.write_bytes(b"mp4")

    def seed(db: Database):
        p_done = db.create_project(
            "https://www.youtube.com/watch?v=abc123def45",
            asdict(ProjectOptions("https://www.youtube.com/watch?v=abc123def45", project_name="YouTube Podcast")),
        )
        db.update_project(p_done, status="completed", progress=100)

        p_fail = db.create_project(
            str(media_file),
            asdict(ProjectOptions(str(media_file), project_name="Local Talk")),
        )
        db.update_project(p_fail, status="failed", progress=48, error="FFmpeg failed\nDetailed codec trace line 2")

        db.create_project(
            str(media_file),
            asdict(ProjectOptions(str(media_file), project_name="Queued Webinar")),
        )

        p_cancel = db.create_project(
            str(media_file),
            asdict(ProjectOptions(str(media_file), project_name="Cancelled Clip")),
        )
        db.update_project(p_cancel, status="cancelled", progress=20, error="Cancelled by user")

    window, _db, _settings = _setup_window(monkeypatch, tmp_path, projects_seed=seed)
    app.processEvents()

    # Compact idle state initially
    assert window.active_job_card.stack.currentIndex() == 0
    # Dynamic overview counters: 0 processing, 1 queued, 1 completed, 1 failed
    assert window.queue_overview._counters["processing"].text() == "0"
    assert window.queue_overview._counters["queued"].text() == "1"
    assert window.queue_overview._counters["completed"].text() == "1"
    assert window.queue_overview._counters["failed"].text() == "1"
    assert "Profile:" in window.queue_overview.profile_badge.text()

    # Collapsible logs panel starts collapsed
    assert not window.logs_panel.drawer.isVisible()
    window.logs_panel.toggle_button.click()
    app.processEvents()
    assert window.logs_panel.drawer.isVisible()

    # Log filtering by severity and search + bounded capacity
    window._log("INFO", "Downloading audio stream")
    window._log("ERROR", "API failed with secret sk-proj-1234567890abcdefghijkl123456")
    window.logs_panel.level_combo.setCurrentText("ERROR")
    app.processEvents()
    shown = window.live_logs.toPlainText()
    assert "Downloading audio stream" not in shown
    assert "sk-proj-1234567890abcdefghijkl123456" not in shown
    assert "[REDACTED]" in shown
    window.logs_panel.level_combo.setCurrentText("All")
    window.logs_panel.search_input.setText("audio")
    app.processEvents()
    assert "Downloading audio stream" in window.live_logs.toPlainText()
    assert "All" in [window.logs_panel.level_combo.itemText(i) for i in range(window.logs_panel.level_combo.count())]
    assert LogView.MAX_LOG_ENTRIES == 4000

    # Filter tabs and error expansion
    failed_tab = next(
        b for b in window.queue_panel.findChildren(QPushButton, "filterTab") if b.text() == "Failed"
    )
    failed_tab.click()
    app.processEvents()
    assert len(window.queue_panel._visible_projects) == 1
    row_card = next(iter(window.queue_panel._cards.values()))
    assert not row_card.details_box.isVisible()
    row_card.toggle_err_btn.click()
    app.processEvents()
    assert row_card.details_box.isVisible()
    assert "Detailed codec trace line 2" in row_card.details_box.text()

    # Search filter
    all_tab = next(
        b for b in window.queue_panel.findChildren(QPushButton, "filterTab") if b.text() == "All"
    )
    all_tab.click()
    window.queue_panel.search_input.setText("YouTube")
    app.processEvents()
    assert len(window.queue_panel._visible_projects) == 1
    assert project_display_name(window.queue_panel._visible_projects[0]) == "YouTube Podcast"
    assert project_source_info(window.queue_panel._visible_projects[0])[0] == "YouTube"

    # Active job state and pipeline progress updates
    window.active_job_card.start_job(
        window.queue_panel._visible_projects[0],
        options_dict={"source": str(media_file), "srt_path": "subs.srt", "face_tracking": True, "captions": True},
        profile_name="balanced",
    )
    window.on_progress(42, "Scoring highlights")
    app.processEvents()
    assert window.active_job_card.stack.currentIndex() == 1
    assert window.progress_bar.value() == 42

    # Responsive narrow width check
    window.resize(760, 560)
    app.processEvents()
    assert window.stack.currentWidget().minimumSizeHint().width() <= window.stack.currentWidget().width()
    window.close()


def test_queuing_pause_resume_and_sequential_dispatch(app, monkeypatch, tmp_path):
    media_file = tmp_path / "video.mp4"
    media_file.write_bytes(b"video")

    window, db, _settings = _setup_window(monkeypatch, tmp_path)
    started = []
    monkeypatch.setattr(window, "_start_worker", lambda pid, opts, **kw: started.append((pid, opts.project_name)))

    # Simulate an active worker while user submits a new project from New Project page
    window.worker_thread = object()
    window.show_page(1)
    window.source_stack.setCurrentIndex(1)
    window.local_source_input.setText(str(media_file))
    window.project_name.setText("Queued Background Job")
    window.generate()

    assert started == []
    queued = db.next_queued_project()
    assert queued is not None
    assert json.loads(queued["options"])["project_name"] == "Queued Background Job"

    # Pause blocks dispatch, Resume triggers next queued project once worker clears
    window.worker_thread = None
    window.toggle_queue_paused(True)
    assert not window.dispatch_next_queued_job()
    assert started == []

    window.toggle_queue_paused(False)
    assert len(started) == 1
    assert started[0] == (queued["id"], "Queued Background Job")

    # Contextual queue actions: cancel queued + remove from history without deleting media
    q2 = db.create_project(str(media_file), asdict(ProjectOptions(str(media_file), project_name="Cancelable Job")))
    window.refresh_all()
    window._handle_queue_action("cancel_queued", q2)
    cancelled_row = next(p for p in db.projects() if p["id"] == q2)
    assert cancelled_row["status"] == "cancelled"

    window._handle_queue_action("remove_history", q2)
    assert all(p["id"] != q2 for p in db.projects())
    assert media_file.exists()
    window.close()
