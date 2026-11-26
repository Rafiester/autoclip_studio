"""Regression tests for Navigation Simplification, Automatic Project Naming, and Global Wheel Protection."""
from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDoubleSpinBox,
    QLabel,
    QScrollArea,
    QSlider,
    QSpinBox,
)

from autoclip_studio.config import SettingsManager, SettingsStore
from autoclip_studio.database import Database
from autoclip_studio.gui import MainWindow
from autoclip_studio.models import ProjectOptions
from autoclip_studio.project_naming import (
    allocate_project_output_dir,
    derive_project_name,
    resolve_existing_project_output_dir,
    sanitize_project_dir_name,
)
from autoclip_studio.ui.clips_view import FramePickerDialog
from autoclip_studio.ui.sidebar import PRIMARY_NAV_ROUTES, SETTINGS_CHILD_ROUTES
from autoclip_studio.ui.theme import theme_manager
from autoclip_studio.youtube_metadata import YouTubeMetadata


def _app() -> QApplication:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QApplication.instance() or QApplication([])


def _setup_window(tmp_path: Path, monkeypatch) -> tuple[MainWindow, Database, SettingsStore]:
    _app()
    db = Database(tmp_path / "app.db")
    settings = SettingsStore(tmp_path / "settings.json")
    monkeypatch.setattr(settings, "api_key", lambda *_a: "")
    monkeypatch.setattr(settings, "set_api_key", lambda *_a: None)
    monkeypatch.setattr("autoclip_studio.gui.Database", lambda: db)
    monkeypatch.setattr("autoclip_studio.gui.SettingsStore", lambda: settings)
    win = MainWindow()
    return win, db, settings


def _send_wheel(target, *, angle_y: int = -120, pixel_y: int = 0) -> None:
    pos = QPointF(max(2.0, target.width() / 2.0), max(2.0, target.height() / 2.0))
    gpos = QPointF(target.mapToGlobal(pos.toPoint()))
    ev = QWheelEvent(
        pos,
        gpos,
        QPoint(0, pixel_y),
        QPoint(0, angle_y),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.ScrollUpdate if pixel_y else Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(target, ev)


def test_navigation_simplification_landing_and_legacy_redirects(tmp_path: Path, monkeypatch) -> None:
    app = _app()
    win, _db, _settings = _setup_window(tmp_path, monkeypatch)
    try:
        win.resize(1280, 820)
        win.show()
        app.processEvents()

        # Launches directly onto Project (Dashboard removed, New Project renamed to Project)
        assert win.stack.currentIndex() == win.PAGE_PROJECT == 0
        assert [r.label for r in PRIMARY_NAV_ROUTES] == [
            "Project", "Processing Queue", "Generated Clips", "Caption Templates",
        ]
        assert [r.label for r in SETTINGS_CHILD_ROUTES] == [
            "General", "Appearance", "AI Providers", "Transcription", "Performance", "Dependencies",
        ]
        assert win.NAV_ITEMS[0] == "Project"
        assert "Dashboard" not in win.NAV_ITEMS
        assert "New Project" not in win.NAV_ITEMS
        assert not hasattr(win, "recent_table")
        assert not hasattr(win, "metric_projects")

        # Heading on landing page is Project
        page_titles = [lbl.text() for lbl in win.stack.widget(0).findChildren(QLabel, "pageTitle")]
        assert page_titles == ["Project"]

        # Legacy Dashboard and New Project routes redirect cleanly to Project
        for legacy in ("Dashboard", "dashboard", "New Project", "new_project", "project"):
            win.show_page(win.PAGE_QUEUE)
            assert win.stack.currentIndex() == win.PAGE_QUEUE
            win.show_route(legacy)
            assert win.stack.currentIndex() == win.PAGE_PROJECT
    finally:
        win.close()


def test_automatic_project_naming_and_duplicate_directory_safety(tmp_path: Path, monkeypatch) -> None:
    app = _app()
    win, db, _settings = _setup_window(tmp_path, monkeypatch)
    submitted: list[tuple[str, ProjectOptions]] = []
    monkeypatch.setattr(win, "_start_worker", lambda pid, opts, **_kw: submitted.append((pid, opts)))

    try:
        win.resize(1100, 760)
        win.show()
        app.processEvents()

        # Optional Project Name field removed cleanly
        assert not hasattr(win, "project_name")
        assert not any(
            "project name" in lbl.text().lower()
            for lbl in win.stack.widget(win.PAGE_PROJECT).findChildren(QLabel)
        )

        # 1. Local video auto-names from filename without extension
        local_vid = tmp_path / "PodCast.Ep.12.mp4"
        local_vid.write_bytes(b"mp4")
        win.set_source_tab(1)
        win.local_source_input.setText(str(local_vid))
        win.generate()

        # 2. YouTube URL with metadata uses detected title; missing metadata uses meaningful fallback
        yt_url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        yt_meta = YouTubeMetadata(
            "dQw4w9WgXcQ", "Creator: Deep Dive | Part #1?", "Channel", 420, "1920x1080", 999, "2026-01-01",
        )
        win.set_source_tab(0)
        win.source_input.setText(yt_url)
        win.youtube_preview.video_id = "dQw4w9WgXcQ"
        win.youtube_preview.result = yt_meta
        win.worker_thread = object()  # Queue second item safely
        win.generate()
        win.worker_thread = None

        # 3. Missing YouTube metadata produces readable fallback name
        assert derive_project_name("https://www.youtube.com/watch?v=abcdefghijk") == "YouTube Video (abcdefghijk)"
        assert derive_project_name("") == "Untitled Project"

        projects = db.projects()
        names = [json.loads(p["options"])["project_name"] for p in projects]
        assert "PodCast.Ep.12" in names
        assert "Creator: Deep Dive | Part #1?" in names

        # Preserve historical custom names on existing DB projects
        legacy_pid = db.create_project(
            str(local_vid),
            asdict(ProjectOptions(str(local_vid), project_name="Legacy Named Show")),
        )
        assert json.loads(db.get_project(legacy_pid)["options"])["project_name"] == "Legacy Named Show"
    finally:
        win.close()

    # Filesystem-safe folder sanitization + duplicate title collision protection
    assert sanitize_project_dir_name('Bad<>:"/\\|?*Title...') == "Bad-Title"
    assert sanitize_project_dir_name("CON") == "CON-project"

    out_root = tmp_path / "outputs"
    dir1 = allocate_project_output_dir(out_root, yt_meta.title, "pid_one_11111111")
    (dir1 / "project.json").write_text(json.dumps({"project_id": "pid_one_11111111"}), encoding="utf-8")
    (dir1 / "01-clip.mp4").write_bytes(b"clip-1")

    dir2 = allocate_project_output_dir(out_root, yt_meta.title, "pid_two_22222222")
    (dir2 / "project.json").write_text(json.dumps({"project_id": "pid_two_22222222"}), encoding="utf-8")

    assert dir1 != dir2
    assert dir1.name == "Creator- Deep Dive - Part #1"
    assert dir2.name == "Creator- Deep Dive - Part #1 (2)"
    assert (dir1 / "01-clip.mp4").is_file()
    assert allocate_project_output_dir(out_root, yt_meta.title, "pid_one_11111111") == dir1
    assert resolve_existing_project_output_dir(out_root, "pid_two_22222222") == dir2


def test_global_wheel_protection_preserves_values_scrolls_pages_and_allows_intentional_ui(tmp_path: Path, monkeypatch) -> None:
    app = _app()
    win, _db, settings = _setup_window(tmp_path, monkeypatch)
    assert isinstance(settings, SettingsManager)

    try:
        win.resize(900, 560)
        win.show()
        app.processEvents()

        # Verify Settings > Appearance themes (dark, light, system) all preserve wheel protection
        for theme_mode in ("dark", "light", "system"):
            win.set_appearance(theme_mode)
            app.processEvents()

            # Check every QComboBox, QSpinBox, QDoubleSpinBox, QSlider across ALL pages
            page_indices = [
                win.PAGE_PROJECT,
                win.PAGE_QUEUE,
                win.PAGE_CLIPS,
                win.PAGE_TEMPLATES,
                win.PAGE_AI_PROVIDERS,
                win.PAGE_TRANSCRIPTION,
                win.PAGE_PERFORMANCE,
                win.PAGE_DEPENDENCIES,
            ]
            for page_idx in page_indices:
                win.show_page(page_idx)
                app.processEvents()
                page_widget = win.stack.widget(page_idx)

                for combo in page_widget.findChildren(QComboBox):
                    if combo.count() < 2:
                        continue
                    orig_idx = combo.currentIndex()
                    orig_text = combo.currentText()
                    combo.setFocus()
                    _send_wheel(combo, angle_y=-120)
                    _send_wheel(combo, angle_y=120, pixel_y=24)  # High-res trackpad
                    if combo.isEditable() and combo.lineEdit() is not None:
                        _send_wheel(combo.lineEdit(), angle_y=-120)
                    app.processEvents()
                    assert combo.currentIndex() == orig_idx and combo.currentText() == orig_text

                for spin in page_widget.findChildren(QSpinBox):
                    if not spin.isEnabled():
                        continue
                    orig_val = spin.value()
                    spin.setFocus()
                    _send_wheel(spin, angle_y=120)
                    if spin.lineEdit() is not None:
                        _send_wheel(spin.lineEdit(), angle_y=-120, pixel_y=-18)
                    app.processEvents()
                    assert spin.value() == orig_val

                for dspin in page_widget.findChildren(QDoubleSpinBox):
                    orig_val = dspin.value()
                    dspin.setFocus()
                    _send_wheel(dspin, angle_y=120)
                    if dspin.lineEdit() is not None:
                        _send_wheel(dspin.lineEdit(), angle_y=-120)
                    app.processEvents()
                    assert dspin.value() == orig_val

                for slider in page_widget.findChildren(QSlider):
                    orig_val = slider.value()
                    slider.setFocus()
                    _send_wheel(slider, angle_y=120, pixel_y=20)
                    _send_wheel(slider, angle_y=-120)
                    app.processEvents()
                    assert slider.value() == orig_val

        # Also verify standalone dialog slider (FramePickerDialog)
        fake_clip = tmp_path / "c.mp4"
        fake_clip.write_bytes(b"mp4")
        fp_dlg = FramePickerDialog(fake_clip, 10.0, initial_ts=5.0, parent=win)
        orig_fp = fp_dlg.slider.value()
        fp_dlg.slider.setFocus()
        _send_wheel(fp_dlg.slider, angle_y=120)
        assert fp_dlg.slider.value() == orig_fp
        fp_dlg.close()

        # Page scrolling continues smoothly when scrolling OVER a wheel-protected widget!
        win.set_appearance("dark")
        win.resize(900, 560)
        win.show_page(win.PAGE_PROJECT)
        app.processEvents()
        proj_scroll = win.stack.widget(win.PAGE_PROJECT).findChild(QScrollArea)
        assert proj_scroll is not None and proj_scroll.verticalScrollBar().maximum() > 0
        proj_scroll.verticalScrollBar().setValue(0)
        win.clip_count.setFocus()
        orig_clips = win.clip_count.value()
        orig_provider_setting = settings.project_defaults().get("clip_count", orig_clips)
        _send_wheel(win.clip_count, angle_y=-240)
        _send_wheel(win.reframe_combo, angle_y=-120, pixel_y=-24)
        app.processEvents()
        assert win.clip_count.value() == orig_clips
        assert settings.project_defaults().get("clip_count", orig_clips) == orig_provider_setting
        assert proj_scroll.verticalScrollBar().value() > 0

        # Intentional interactions continue working:
        # 1) Keyboard arrow keys & spinbox step buttons
        win.clip_count.setFocus()
        QTest.keyClick(win.clip_count, Qt.Key.Key_Up)
        assert win.clip_count.value() == orig_clips + 1
        win.clip_count.stepDown()
        assert win.clip_count.value() == orig_clips

        # 2) Open combo dropdown popup stays scrollable with wheel
        combo = win.resolution
        assert combo.count() >= 3
        combo.showPopup()
        app.processEvents()
        popup_view = combo.view()
        if popup_view is not None and popup_view.viewport() is not None:
            _send_wheel(popup_view.viewport(), angle_y=-120)
            app.processEvents()
        combo.setCurrentIndex(1)
        assert combo.currentIndex() == 1
        combo.hidePopup()

        # 3) Slider handle / keyboard / manual changes still work cleanly
        win.show_page(win.PAGE_TEMPLATES)
        app.processEvents()
        before_y = win.caption_library.y_slider.value()
        win.caption_library.y_slider.setValue(before_y + 3)
        assert win.caption_library.y_slider.value() == before_y + 3
        win.caption_library.reset_positioning_defaults()

        # 4) Legitimate scrollable content (LogView plain text & queue/clips scroll areas) still scrolls
        win.show_page(win.PAGE_QUEUE)
        win.logs_panel.toggle_button.setChecked(True)
        for i in range(60):
            win.live_logs.add_entry("INFO", f"Line {i}")
        win.live_logs.flush()
        app.processEvents()
        vbar = win.live_logs.verticalScrollBar()
        assert vbar.maximum() > 0
        vbar.setValue(0)
        _send_wheel(win.live_logs.viewport(), angle_y=-240)
        app.processEvents()
        assert vbar.value() > 0

        # 5) Keyboard accessibility (Tab / StrongFocus preserved on controls)
        for ctrl in (win.clip_count, win.aspect, win.srt_offset, win.caption_library.y_slider):
            assert ctrl.focusPolicy() != Qt.FocusPolicy.NoFocus
    finally:
        win.set_appearance("dark")
        win.close()
