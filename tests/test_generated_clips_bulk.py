from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from autoclip_studio.clip_cleanup import delete_clips_transaction, open_clip_file, reveal_in_file_manager
from autoclip_studio.config import SettingsStore
from autoclip_studio.database import Database
from autoclip_studio.gui import MainWindow
from autoclip_studio.thumbnails import generate_clip_thumbnail
from autoclip_studio.ui.clips_workspace import ClipDeleteDialog
from autoclip_studio.ui.theme import theme_manager


def _app() -> QApplication:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QApplication.instance() or QApplication([])


def _make_video(path: Path, *, frames: int = 36) -> None:
    import cv2
    import numpy as np

    path.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 12.0, (180, 320))
    assert writer.isOpened()
    try:
        for i in range(frames):
            frame = np.full((320, 180, 3), 65 + (i % 120), dtype=np.uint8)
            cv2.rectangle(frame, (30, 60), (150, 220), (40, 180, 230), 2)
            writer.write(frame)
    finally:
        writer.release()


def _setup_window(tmp_path: Path, monkeypatch):
    _app()
    store = SettingsStore(tmp_path / "settings.json")
    monkeypatch.setattr(store, "api_key", lambda *_a: "")
    monkeypatch.setattr(store, "set_api_key", lambda *_a: None)
    db = Database(tmp_path / "clips_bulk.db")
    monkeypatch.setattr("autoclip_studio.gui.SettingsStore", lambda: store)
    monkeypatch.setattr("autoclip_studio.gui.Database", lambda: db)
    return store, db, MainWindow()


def test_clip_selection_mode_independent_of_playback_and_filter_scopes(tmp_path: Path, monkeypatch) -> None:
    _, db, win = _setup_window(tmp_path, monkeypatch)
    c1 = tmp_path / "out" / "01-alpha.mp4"
    c2 = tmp_path / "out" / "02-beta.mp4"
    c3_missing = tmp_path / "out" / "03-missing-alpha.mp4"
    _make_video(c1)
    _make_video(c2)
    t1 = generate_clip_thumbnail(c1, tmp_path / "out" / "thumbnails", title="Alpha Clip")
    t2 = generate_clip_thumbnail(c2, tmp_path / "out" / "thumbnails", title="Beta Clip")

    pid = db.create_project("source.mp4", {"project_name": "Podcast"})
    cid1 = db.add_clip(pid, str(c1), "Alpha Clip", "H1", "First desc", 90, 10.0, thumbnail_path=str(t1.preview_path), thumbnail_raw_path=str(t1.raw_path))
    cid2 = db.add_clip(pid, str(c2), "Beta Clip", "H2", "Second desc", 70, 20.0, thumbnail_path=str(t2.preview_path), thumbnail_raw_path=str(t2.raw_path))
    cid3 = db.add_clip(pid, str(c3_missing), "Missing Alpha", "H3", "Gone file", 80, 15.0)

    try:
        win.show()
        win.show_page(3)
        _app().processEvents()

        # Checkboxes hidden outside selection mode
        assert not win.clips_library.selection_bar.isVisible()
        assert all(not card.select_checkbox.isVisible() for card in win.clips_library._cards.values())

        # Pin preview on cid1 and verify toggling selection checkboxes does NOT change active preview/playback
        win.clips_library.select_clip_by_id(cid1)
        _app().processEvents()
        initial_preview_id = win.clips_library.selected_clip()["id"]

        win.clips_library.select_clips_btn.setChecked(True)
        _app().processEvents()
        assert win.clips_library.selection_bar.isVisible()
        assert all(card.select_checkbox.isVisible() for card in win.clips_library._cards.values())

        win.clips_library._cards[cid2].select_checkbox.setChecked(True)
        win.clips_library._cards[cid3].select_checkbox.setChecked(True)
        _app().processEvents()
        assert win.clips_library.selected_clip()["id"] == initial_preview_id
        assert set(win.clips_library.checked_clip_ids()) == {cid2, cid3}
        assert "2 selected" in win.clips_library.selection_count_label.text()
        assert win.clips_library.action_bulk_delete.isEnabled()

        # Grid view preserves checkboxes, full card height & independent check state
        win.clips_library.set_view_mode("grid")
        _app().processEvents()
        assert win.clips_library._cards[cid2].select_checkbox.isVisible()
        assert win.clips_library._cards[cid2].select_checkbox.isChecked()
        assert win.clips_library._cards[cid2].height() >= 220
        win.clips_library.set_view_mode("list")

        # Search filter distinguishes visible selection vs full library
        win.clips_library.clear_checked()
        assert not win.clips_library.action_bulk_delete.isEnabled()
        win.clips_library.search_input.setText("Alpha")
        _app().processEvents()
        assert "Select Visible (2)" in win.clips_library.select_all_btn.text()
        assert win.clips_library.action_select_entire.isVisible()

        win.clips_library.select_all_visible()
        assert set(win.clips_library.checked_clip_ids()) == {cid1, cid3}

        # Select Missing Files only picks missing item in active visible filter scope
        win.clips_library.clear_checked()
        win.clips_library.select_missing_files()
        assert set(win.clips_library.checked_clip_ids()) == {cid3}

        # Select All in Library selects everything even outside current filter
        win.clips_library.select_all_in_library()
        assert set(win.clips_library.checked_clip_ids()) == {cid1, cid2, cid3}

        # Done exits selection mode cleanly and hides checkboxes
        win.clips_library.done_selection_btn.click()
        _app().processEvents()
        assert not win.clips_library.selection_bar.isVisible()
        assert win.clips_library.checked_clip_ids() == []
        assert all(not card.select_checkbox.isVisible() for card in win.clips_library._cards.values())
    finally:
        win.close()


def test_missing_file_states_individual_menus_and_quick_footer(tmp_path: Path, monkeypatch) -> None:
    _, db, win = _setup_window(tmp_path, monkeypatch)
    c_ok = tmp_path / "clips" / "ok.mp4"
    c_gone = tmp_path / "clips" / "gone.mp4"
    _make_video(c_ok)
    _make_video(c_gone)
    pid = db.create_project("source.mp4", {})
    cid_ok = db.add_clip(pid, str(c_ok), "Ready Clip", "", "Valid", 85, 4.0)
    cid_gone = db.add_clip(pid, str(c_gone), "Soon Gone", "", "Will disappear", 75, 4.0)

    try:
        win.resize(920, 680)
        win.show()
        win.show_page(3)
        _app().processEvents()

        # Wait for initial thumbnail extraction to complete, then leave Generated Clips so media handle closes
        deadline = time.monotonic() + 5.0
        while (win.thumb_runner.is_busy(cid_ok) or win.thumb_runner.is_busy(cid_gone)) and time.monotonic() < deadline:
            _app().processEvents()
            time.sleep(0.02)
        win.show_page(0)
        _app().processEvents()
        c_gone.unlink()
        win.show_page(3)
        _app().processEvents()

        gone_card = win.clips_library._cards[cid_gone]
        ok_card = win.clips_library._cards[cid_ok]
        assert not gone_card.action_card_open.isEnabled()
        assert gone_card.action_card_folder.isEnabled()
        assert gone_card.action_card_delete.isEnabled()
        assert ok_card.action_card_open.isEnabled()
        assert gone_card._missing and not ok_card._missing

        # Selecting missing clip disables playback & Open File while keeping Open Folder enabled
        win.clips_library.select_clip_by_id(cid_gone)
        _app().processEvents()
        assert not win.clip_detail.primary_btn.isEnabled()
        assert not win.clip_detail.play_btn.isEnabled()
        assert win.clip_detail.open_folder_btn.isEnabled()
        assert win.clip_detail.more_btn.isEnabled()

        # Redesigned footer has Open File, Open Folder, and icon-only three-dot button (no "Actions" text)
        win.clips_library.select_clip_by_id(cid_ok)
        _app().processEvents()
        assert win.clip_detail.primary_btn.text() == "Open File"
        assert win.clip_detail.open_folder_btn.text() == "Open Folder"
        assert win.clip_detail.more_btn.text() == ""
        assert win.clip_detail.primary_btn.isEnabled()
        assert win.clip_detail.open_folder_btn.isEnabled()

        # Theme & small-window footer, toolbar, player preview, and grid cards never overlap or clip
        win.resize(920, 680)
        for theme_name in ("light", "dark"):
            theme_manager().apply(theme_name)
            win.clips_library.set_view_mode("grid")
            _app().processEvents()
            assert win.clips_library.scroll.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
            assert win.clip_detail.primary_btn.geometry().right() < win.clip_detail.open_folder_btn.geometry().left() + 2
            assert win.clip_detail.open_folder_btn.geometry().right() < win.clip_detail.more_btn.geometry().left() + 2
            assert win.clip_detail.stage.thumb_preview.width() <= win.clip_detail.stage.stack.width()
            assert win.clips_library.select_clips_btn.width() >= win.clips_library.select_clips_btn.sizeHint().width() - 2
            assert win.clips_library.export_all_btn.width() >= win.clips_library.export_all_btn.sizeHint().width() - 2
            cards_sorted = sorted(win.clips_library._cards.values(), key=lambda w: (w.y(), w.x()))
            if len(cards_sorted) >= 2 and cards_sorted[0].x() == cards_sorted[1].x():
                assert cards_sorted[0].y() + cards_sorted[0].height() <= cards_sorted[1].y()
        win.clips_library.set_view_mode("list")

        # Verify Open File and Reveal in Folder platform helpers safely
        opened: list[str] = []
        monkeypatch.setattr("autoclip_studio.clip_cleanup.QDesktopServices.openUrl", lambda u: opened.append(u.toLocalFile()) or True)
        launched: list[list[str]] = []
        monkeypatch.setattr("autoclip_studio.clip_cleanup.subprocess.Popen", lambda cmd, **_kw: launched.append(list(cmd)))

        assert open_clip_file(c_ok)
        assert not open_clip_file(c_gone)
        assert Path(opened[-1]) == c_ok.resolve()

        assert reveal_in_file_manager(c_ok)
        if sys.platform == "win32":
            assert any("explorer.exe" in cmd[0] and "/select," in cmd[1] for cmd in launched)
        elif sys.platform == "darwin":
            assert any(cmd[:2] == ["open", "-R"] for cmd in launched)

        assert reveal_in_file_manager(c_gone)  # Parent folder exists -> opens containing folder
    finally:
        theme_manager().apply("dark")
        win.close()


def test_safe_deletion_protected_assets_partial_failure_and_persistence(tmp_path: Path, monkeypatch) -> None:
    store, db, win = _setup_window(tmp_path, monkeypatch)

    # Create source video, shared cache download & transcript, 3 clips, shared thumb & project.json
    source_video = tmp_path / "source_orig.mp4"
    source_video.write_bytes(b"original-source-do-not-delete")
    cache_dl = tmp_path / "cache" / "downloads" / "yt123.mp4"
    cache_tr = tmp_path / "cache" / "transcripts" / "tr123.json"
    cache_dl.parent.mkdir(parents=True, exist_ok=True)
    cache_tr.parent.mkdir(parents=True, exist_ok=True)
    cache_dl.write_bytes(b"shared-download")
    cache_tr.write_text('{"segments": []}', encoding="utf-8")

    proj_dir = tmp_path / "output" / "proj1"
    c1 = proj_dir / "01-keep.mp4"
    c2 = proj_dir / "02-remove-lib-only.mp4"
    c3 = proj_dir / "03-trash-file.mp4"
    for p in (c1, c2, c3):
        _make_video(p)
    t1 = generate_clip_thumbnail(c1, proj_dir / "thumbnails", title="Keep")
    t3 = generate_clip_thumbnail(c3, proj_dir / "thumbnails", title="Trash")
    c4_missing = proj_dir / "04-stale-missing.mp4"

    manifest_path = proj_dir / "project.json"
    manifest_path.write_text(json.dumps({
        "project_id": "proj1",
        "clips": [{"path": str(c1)}, {"path": str(c2)}, {"path": str(c3)}],
    }), encoding="utf-8")

    pid = db.create_project(str(source_video), {"project_name": "Pod"})
    cid1 = db.add_clip(pid, str(c1), "Keep Clip", "", "", 90, 10.0, thumbnail_path=str(t1.preview_path), thumbnail_raw_path=str(t1.raw_path))
    cid2 = db.add_clip(pid, str(c2), "Record Only", "", "", 70, 20.0, thumbnail_path=str(t1.preview_path))  # Shares t1 preview!
    cid3 = db.add_clip(pid, str(c3), "Trash Clip", "", "", 80, 30.0, thumbnail_path=str(t3.preview_path), thumbnail_raw_path=str(t3.raw_path))
    cid4 = db.add_clip(pid, str(c4_missing), "Stale Record", "", "", 60, 10.0)

    try:
        win.show()
        win.show_page(3)
        _app().processEvents()

        # Dialog confirmation stats accuracy & Cancel safety
        dlg = ClipDeleteDialog([db.get_clip(cid3), db.get_clip(cid4)], win)
        dlg_labels = " | ".join(lbl.text() for lbl in dlg.findChildren(type(win.clip_detail.title_label)))
        assert "Selected clips: 2" in dlg_labels
        assert "Existing exported media files: 1" in dlg_labels
        assert "Missing-file records: 1" in dlg_labels
        dlg.close()

        monkeypatch.setattr("autoclip_studio.ui.clips_workspace.ClipDeleteDialog.exec", lambda _self: QDialog.DialogCode.Rejected)
        win.clips_library.select_clip_by_id(cid3)
        win.delete_selected_clip()
        assert db.clip_count() == 4  # Cancel leaves everything untouched

        # 1. Remove from Library only keeps physical file c2 on disk
        win.execute_clip_deletion([db.get_clip(cid2)], delete_files=False)
        assert c2.is_file()
        assert t1.preview_path.is_file()
        assert db.get_clip(cid2) is None
        assert db.clip_count() == 3

        # 2. Partial failure: fail trashing c3, succeed cleaning stale cid4 -> c3 stays in DB!
        fail_attempts = []
        report_partial = delete_clips_transaction(
            [db.get_clip(cid3), db.get_clip(cid4)],
            db,
            delete_files=True,
            trash_fn=lambda p: (fail_attempts.append(p) or False, "Locked") if p == c3 else (True, ""),
            protected_roots=(tmp_path / "cache",),
        )
        assert cid3 not in report_partial.deleted_ids
        assert cid4 in report_partial.deleted_ids
        assert len(report_partial.failed_items) == 1
        assert db.get_clip(cid3) is not None  # Never drop DB row on failed deletion!
        assert db.get_clip(cid4) is None

        # 3. Delete currently previewed clip c3 with Trash deletion: releases handle & prunes manifest
        win.clips_library.select_clip_by_id(cid3)
        _app().processEvents()
        win.execute_clip_deletion([db.get_clip(cid3)], delete_files=True)
        assert not c3.exists()
        assert not t3.preview_path.exists()
        assert not t3.raw_path.exists()
        assert win.clip_detail.player.source().isEmpty() or Path(win.clip_detail.player.source().toLocalFile()) != c3
        manifest_after = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert not any(Path(entry["path"]) == c3 for entry in manifest_after["clips"])

        # Protected files and unrelated clip/source/cache remain untouched!
        assert source_video.is_file()
        assert cache_dl.is_file()
        assert cache_tr.is_file()
        assert c1.is_file()
        assert t1.preview_path.is_file()
        assert proj_dir.is_dir()
        assert db.project_count() == 1

        # Summary stats refresh cleanly to remaining clip cid1 (score 90, duration 00:10)
        win.refresh_all()
        assert win.clips_library.card_count() == 1
        assert win.clips_library.summary_count.findChild(type(win.clip_detail.title_label), "summaryValue").text() == "1"
        assert "00:10" in win.clips_library.summary_duration.findChild(type(win.clip_detail.title_label), "summaryValue").text()
        assert "90/100" in win.clips_library.summary_score.findChild(type(win.clip_detail.title_label), "summaryValue").text()

        # Delete final clip -> clean empty state & 0 summary stats persisted across restart
        win.execute_clip_deletion([db.get_clip(cid1)], delete_files=True)
        assert win.clips_library.card_count() == 0
        assert win.clips_library.summary_count.findChild(type(win.clip_detail.title_label), "summaryValue").text() == "0"
    finally:
        win.close()

    # Restart verification against same SQLite DB
    reloaded_db = Database(tmp_path / "clips_bulk.db")
    assert reloaded_db.clip_count() == 0
    assert reloaded_db.project_count() == 1
