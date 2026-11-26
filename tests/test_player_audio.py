"""Regression tests for embedded Generated Clips video player audio playback and persistence."""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path

import pytest
from PySide6.QtMultimedia import QMediaDevices, QMediaPlayer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from autoclip_studio.config import SettingsStore
from autoclip_studio.database import Database
from autoclip_studio.gui import MainWindow
from autoclip_studio.models import ProjectOptions
from autoclip_studio.process import find_media_tool
from autoclip_studio.ui.clips_workspace import ClipDetailPlayerPanel
from autoclip_studio.ui.theme import theme_manager


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _wait_until(predicate, timeout_s: float = 4.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        QTest.qWait(30)
        if predicate():
            return True
    return bool(predicate())


def _make_av_clip(path: Path, *, freq: int = 440, duration: float = 3.0) -> Path:
    """Create a standard 9:16 H.264 + 48 kHz stereo AAC MP4 matching AutoClip Studio exports."""
    ffmpeg = find_media_tool("ffmpeg")
    subprocess.run(
        [
            ffmpeg, "-y", "-v", "error",
            "-f", "lavfi", "-i", f"testsrc2=size=360x640:rate=25:duration={duration}",
            "-f", "lavfi", "-i", f"sine=frequency={freq}:sample_rate=48000:duration={duration}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "ultrafast",
            "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "128k",
            "-movflags", "+faststart",
            str(path),
        ],
        check=True,
    )
    return path


def test_macos_selects_native_darwin_multimedia_backend() -> None:
    if sys.platform == "darwin":
        assert os.environ.get("QT_MEDIA_BACKEND") == "darwin"


def test_embedded_player_audio_lifecycle_controls_fullscreen_and_device_sync(tmp_path: Path, caplog) -> None:
    app = _app()
    clip1 = _make_av_clip(tmp_path / "clip1.mp4", freq=440, duration=3.0)
    clip2 = _make_av_clip(tmp_path / "clip2.mp4", freq=660, duration=3.0)

    panel = ClipDetailPlayerPanel()
    panel.resize(420, 720)
    panel.show()
    app.processEvents()

    try:
        # 1. Persistent QAudioOutput attached to QMediaPlayer, initialized unmuted at 0.8
        assert panel.player.audioOutput() is panel.audio_output
        assert panel.audio_output.parent() is panel
        assert panel.player.parent() is panel
        assert panel.audio_output.volume() == pytest.approx(0.8, abs=0.02)
        assert not panel.audio_output.isMuted()
        assert panel.volume_slider.value() == 80
        if QMediaDevices.audioOutputs():
            assert not panel.audio_output.device().isNull()

        # 2. Load clip 1 & initial playback with audio track detection + selection
        native_gui = app.platformName() != "offscreen"
        panel.display_clip({"id": "c1", "title": "Clip 1", "path": str(clip1), "duration": 3.0, "score": 92})
        if native_gui:
            assert _wait_until(
                lambda: panel.player.mediaStatus() in (
                    QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia,
                )
            )
        panel.toggle_play()
        if native_gui:
            assert _wait_until(lambda: panel.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState)
            assert _wait_until(lambda: panel.player.hasAudio() and len(panel.player.audioTracks()) >= 1)
            assert panel.player.activeAudioTrack() >= 0
            assert _wait_until(lambda: panel.player.position() > 150)

            # 3. Play / Pause
            panel.toggle_play()
            assert _wait_until(lambda: panel.player.playbackState() == QMediaPlayer.PlaybackState.PausedState)
            panel.toggle_play()
            assert _wait_until(lambda: panel.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState)

            # 4. Seeking preserves audio output binding
            panel.player.setPosition(1200)
            assert _wait_until(lambda: panel.player.position() >= 1000)
        assert panel.player.audioOutput() is panel.audio_output

        # 5. Volume normalization 0-100 -> 0.0-1.0 & Mute/Unmute sync
        recorded: list[tuple[int, bool]] = []
        panel.audio_settings_changed.connect(lambda v, m: recorded.append((v, m)))
        panel.volume_slider.setValue(35)
        app.processEvents()
        assert panel.audio_output.volume() == pytest.approx(0.35, abs=0.02)
        assert not panel.audio_output.isMuted()

        panel.toggle_mute()
        app.processEvents()
        assert panel.audio_output.isMuted()
        panel.toggle_mute()
        app.processEvents()
        assert not panel.audio_output.isMuted()
        assert panel.audio_output.volume() == pytest.approx(0.35, abs=0.02)

        # Dragging to 0 then clicking unmute restores last nonzero volume
        panel.volume_slider.setValue(0)
        app.processEvents()
        assert panel.audio_output.volume() == pytest.approx(0.0, abs=0.01)
        panel.toggle_mute()
        app.processEvents()
        assert not panel.audio_output.isMuted()
        assert panel.volume_slider.value() == 35
        assert panel.audio_output.volume() == pytest.approx(0.35, abs=0.02)
        assert recorded

        # 6. Fullscreen enter & exit preserves single player, volume, mute, and active audio
        panel.toggle_fullscreen()
        app.processEvents()
        assert panel._fullscreen_win is not None
        assert panel.player.audioOutput() is panel.audio_output
        assert panel._fullscreen_win.vol_slider.value() == 35
        panel._fullscreen_win.vol_slider.setValue(65)
        app.processEvents()
        assert panel.volume_slider.value() == 65
        assert panel.audio_output.volume() == pytest.approx(0.65, abs=0.02)

        panel.toggle_fullscreen()
        app.processEvents()
        assert panel._fullscreen_win is None
        assert panel.player.audioOutput() is panel.audio_output
        assert panel.audio_output.volume() == pytest.approx(0.65, abs=0.02)
        assert not panel.audio_output.isMuted()

        # 7. Theme switching does not mute or detach audio
        theme_manager().apply("light")
        app.processEvents()
        theme_manager().apply("dark")
        app.processEvents()
        assert panel.player.audioOutput() is panel.audio_output
        assert panel.audio_output.volume() == pytest.approx(0.65, abs=0.02)
        assert not panel.audio_output.isMuted()

        # 8. Switching generated clips stops clip 1 cleanly and loads clip 2 with audio ready
        panel.display_clip({"id": "c2", "title": "Clip 2", "path": str(clip2), "duration": 3.0, "score": 88})
        assert panel.player.playbackState() == QMediaPlayer.PlaybackState.StoppedState
        if native_gui:
            assert _wait_until(
                lambda: panel.player.mediaStatus() in (
                    QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia,
                )
            )
            panel.toggle_play()
            assert _wait_until(lambda: panel.player.hasAudio() and panel.player.activeAudioTrack() >= 0)
        assert panel.audio_output.volume() == pytest.approx(0.65, abs=0.02)

        # 9. System audio device change callback preserves volume, mute, and player binding
        panel._on_audio_outputs_changed()
        app.processEvents()
        assert panel.player.audioOutput() is panel.audio_output
        assert panel.audio_output.volume() == pytest.approx(0.65, abs=0.02)

        # 10. Diagnostic logging on multimedia errors
        with caplog.at_level(logging.ERROR, logger="autoclip"):
            panel._on_error(QMediaPlayer.Error.FormatError, "Simulated audio backend failure")
        assert any("Simulated audio backend failure" in rec.message for rec in caplog.records)
        assert "Playback error:" in panel.desc_label.text()
    finally:
        panel.release_media()
        panel.close()


def test_player_volume_persists_across_restart_and_page_navigation(tmp_path: Path, monkeypatch) -> None:
    app = _app()
    clip = _make_av_clip(tmp_path / "persist.mp4", freq=520, duration=2.5)
    db = Database(tmp_path / "app.db")
    settings_path = tmp_path / "settings.json"
    settings1 = SettingsStore(settings_path)
    monkeypatch.setattr(settings1, "api_key", lambda *_a: "")
    monkeypatch.setattr(settings1, "set_api_key", lambda *_a: None)
    monkeypatch.setattr("autoclip_studio.gui.Database", lambda: db)
    monkeypatch.setattr("autoclip_studio.gui.SettingsStore", lambda: settings1)

    pid = db.create_project(str(clip), asdict(ProjectOptions(source=str(clip), project_name="Audio Test")))
    db.update_project(pid, status="completed", progress=100)
    db.add_clip(pid, str(clip), "Persisted Audio Clip", "Hook", "Desc", 90, 2.5)

    native_gui = app.platformName() != "offscreen"
    win1 = MainWindow()
    try:
        win1.show()
        win1.show_page(win1.PAGE_CLIPS)
        app.processEvents()
        if native_gui:
            assert _wait_until(
                lambda: win1.clip_detail.player.mediaStatus() in (
                    QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia,
                )
            )
        win1.clip_detail.volume_slider.setValue(42)
        app.processEvents()
        win1.clip_detail.toggle_play()
        if native_gui:
            assert _wait_until(lambda: win1.clip_detail.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState)

        # Navigating away from Generated Clips stops playback cleanly (no background audio bleed)
        win1.show_page(win1.PAGE_PROJECT)
        app.processEvents()
        assert win1.clip_detail.player.playbackState() == QMediaPlayer.PlaybackState.StoppedState

        # Returning to Generated Clips restores playable audio with saved volume
        win1.show_page(win1.PAGE_CLIPS)
        app.processEvents()
        win1.clip_detail.toggle_play()
        if native_gui:
            assert _wait_until(lambda: win1.clip_detail.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState)
        assert win1.clip_detail.audio_output.volume() == pytest.approx(0.42, abs=0.02)
    finally:
        win1.close()
        app.processEvents()

    # Restart simulation: reload SettingsStore from disk into a fresh MainWindow
    settings2 = SettingsStore(settings_path)
    monkeypatch.setattr(settings2, "api_key", lambda *_a: "")
    monkeypatch.setattr(settings2, "set_api_key", lambda *_a: None)
    monkeypatch.setattr("autoclip_studio.gui.SettingsStore", lambda: settings2)
    assert settings2.ui_state()["player_volume"] == 42
    assert settings2.ui_state()["player_muted"] is False

    win2 = MainWindow()
    try:
        assert win2.clip_detail.volume_slider.value() == 42
        assert win2.clip_detail.audio_output.volume() == pytest.approx(0.42, abs=0.02)
        assert not win2.clip_detail.audio_output.isMuted()
    finally:
        win2.close()
        app.processEvents()
