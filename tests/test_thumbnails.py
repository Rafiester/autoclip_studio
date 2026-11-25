from __future__ import annotations

import os
from pathlib import Path

import cv2
import numpy as np
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from autoclip_studio.database import Database
from autoclip_studio.process import ProcessManager, find_media_tool
from autoclip_studio.thumbnails import (
    export_thumbnail, generate_clip_thumbnail, score_frame,
)
from autoclip_studio.ui.clips_view import FramePickerDialog


def _app() -> QApplication:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QApplication.instance() or QApplication([])


def _make_test_clip(path: Path, *, dark: bool = False) -> None:
    manager = ProcessManager()
    src = "color=c=black:size=360x640:rate=15:duration=3" if dark else "testsrc2=size=360x640:rate=15:duration=3"
    manager.run([
        find_media_tool("ffmpeg"), "-hide_banner", "-y",
        "-f", "lavfi", "-i", src,
        "-c:v", "libx264", "-pix_fmt", "yuv420p", path,
    ])


def test_frame_scoring_rejects_black_and_prefers_sharp_over_blur() -> None:
    black = np.zeros((320, 180, 3), dtype=np.uint8)
    _, _, usable_black = score_frame(black)
    assert not usable_black

    sharp = np.full((320, 180, 3), 45, dtype=np.uint8)
    cv2.rectangle(sharp, (20, 40), (160, 280), (220, 220, 220), 3)
    cv2.putText(sharp, "HOOK", (35, 170), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (240, 80, 80), 3)
    blurry = cv2.GaussianBlur(sharp, (21, 21), 8)

    s_sharp, _, ok_sharp = score_frame(sharp)
    s_blur, _, ok_blur = score_frame(blurry)
    assert ok_sharp and ok_blur
    assert s_sharp > s_blur


def test_auto_thumbnail_generation_dark_fallback_and_caching(tmp_path: Path) -> None:
    clip = tmp_path / "01-demo.mp4"
    _make_test_clip(clip, dark=False)
    orig_stat = clip.stat()

    res = generate_clip_thumbnail(clip, tmp_path / "thumbnails", title="Demo Clip", profile="balanced")
    assert res.preview_path.is_file()
    assert res.raw_path.is_file()
    assert clip.stat().st_mtime_ns == orig_stat.st_mtime_ns

    preview_img = QImage(str(res.preview_path))
    assert not preview_img.isNull()
    assert preview_img.height() > preview_img.width()  # 9:16 vertical preserved

    # Cache hit skips regeneration
    raw_mtime = res.raw_path.stat().st_mtime_ns
    cached = generate_clip_thumbnail(
        clip, tmp_path / "thumbnails", title="Demo Clip", profile="balanced",
        existing_cache_key=res.cache_key,
    )
    assert cached.raw_path.stat().st_mtime_ns == raw_mtime

    # Dark/black clip falls back to midpoint without crashing
    dark_clip = tmp_path / "02-dark.mp4"
    _make_test_clip(dark_clip, dark=True)
    dark_res = generate_clip_thumbnail(dark_clip, tmp_path / "thumbnails", title="Dark", profile="eco")
    assert dark_res.preview_path.is_file()
    assert dark_res.raw_path.is_file()


def test_manual_frame_selection_and_1080x1920_export(tmp_path: Path) -> None:
    _app()
    clip = tmp_path / "03-manual.mp4"
    _make_test_clip(clip, dark=False)

    dialog = FramePickerDialog(clip, 3.0, initial_ts=1.2)
    try:
        assert abs(dialog.selected_timestamp - 1.2) < 0.05
    finally:
        dialog.close()

    res = generate_clip_thumbnail(clip, tmp_path / "thumbnails", title="Manual", timestamp=1.5, force=True)
    assert abs(res.timestamp - 1.5) < 0.01

    exported = export_thumbnail(
        res.raw_path, tmp_path / "exported_1080x1920.jpg",
        title="MY VIRAL CLIP", overlay_title=True, size=(1080, 1920),
    )
    assert exported.is_file()
    img = QImage(str(exported))
    assert img.width() == 1080 and img.height() == 1920


def test_database_thumbnail_persistence_and_clip_deletion(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AUTOCLIP_DATA_DIR", str(tmp_path / "data"))
    db = Database(tmp_path / "test.db")
    pid = db.create_project("source.mp4", {})
    clip_path = tmp_path / "clip.mp4"
    thumb_path = tmp_path / "clip.thumb.jpg"
    raw_path = tmp_path / "clip.raw.jpg"
    for p in (clip_path, thumb_path, raw_path):
        p.write_bytes(b"data")

    cid = db.add_clip(
        pid, str(clip_path), "Title", "Hook", "Desc", 90, 30.0,
        thumbnail_path=str(thumb_path), thumbnail_raw_path=str(raw_path),
        thumbnail_timestamp=12.5, thumbnail_cache_key="abc123",
    )
    clips = db.clips()
    assert len(clips) == 1
    assert clips[0]["thumbnail_path"] == str(thumb_path)
    assert clips[0]["thumbnail_timestamp"] == 12.5

    db.update_clip_thumbnail(cid, str(thumb_path), str(raw_path), 15.0, "def456")
    assert db.clips()[0]["thumbnail_cache_key"] == "def456"

    removed = db.delete_clip(cid)
    assert removed is not None
    assert db.clips() == []


def test_generated_clips_ui_displays_thumbnail_and_actions(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AUTOCLIP_DATA_DIR", str(tmp_path / "data"))
    app = _app()
    from autoclip_studio.gui import MainWindow

    clip_path = tmp_path / "01-ui.mp4"
    _make_test_clip(clip_path, dark=False)
    thumb = generate_clip_thumbnail(clip_path, tmp_path / "thumbnails", title="UI Clip")

    window = MainWindow()
    try:
        pid = window.database.create_project("source.mp4", {})
        window.database.add_clip(
            pid, str(clip_path), "UI Clip", "Hook", "Desc", 88, 3.0,
            thumbnail_path=str(thumb.preview_path),
            thumbnail_raw_path=str(thumb.raw_path),
            thumbnail_timestamp=thumb.timestamp,
            thumbnail_cache_key=thumb.cache_key,
        )
        window.show_page(3)
        window.clips_table.selectRow(0)
        app.processEvents()

        assert window.regen_thumb_button.isEnabled()
        assert window.pick_frame_button.isEnabled()
        assert window.export_thumb_button.isEnabled()
        assert window.delete_clip_button.isEnabled()
        assert "Thumbnail ready" in window.clip_meta.text()
    finally:
        window.close()
