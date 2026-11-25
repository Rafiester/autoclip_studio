import json
import os
from subprocess import CalledProcessError, CompletedProcess
from threading import Event
from time import monotonic

import pytest
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from autoclip_studio import youtube_metadata
from autoclip_studio.config import SettingsStore
from autoclip_studio.database import Database
from autoclip_studio.gui import MainWindow
from autoclip_studio.media import acquire_source, youtube_id, youtube_source_error, youtube_watch_url
from autoclip_studio.process import ProcessManager
from autoclip_studio.youtube_metadata import YouTubeMetadata, YouTubeMetadataError, fetch_youtube_metadata


VIDEO = "aqz-KE-bpKQ"
OTHER = "dQw4w9WgXcQ"


def test_video_url_aliases_and_playlist_validation():
    for url in (youtube_watch_url(VIDEO), f"https://youtu.be/{VIDEO}?si=example",
                f"https://www.youtube.com/shorts/{VIDEO}", f"{youtube_watch_url(VIDEO)}&list=PL123"):
        assert youtube_id(url) == VIDEO
        assert not youtube_source_error(url)
    playlist = "https://www.youtube.com/playlist?list=PL123"
    assert youtube_id(playlist) is None
    with pytest.raises(ValueError, match="Playlist links"):
        acquire_source(playlist, True, ProcessManager())
    assert youtube_source_error("https://youtube.com/watch?v=too-short")
    assert not youtube_source_error("https://youtube.com/@example/live")


def test_extracts_metadata_without_media_and_reuses_cache(monkeypatch, tmp_path):
    requests = []
    info = {
        "id": VIDEO, "title": "Sample video", "channel": "Sample channel", "duration": 5076,
        "upload_date": "20240601", "view_count": 1234, "formats": [
            {"width": 720, "height": 1280, "vcodec": "avc1"},
            {"width": 1080, "height": 1920, "vcodec": "vp9"},
            {"width": 3840, "height": 2160, "protocol": "mhtml"},
            {"width": 3840, "height": 2160, "vcodec": "none"},
        ], "thumbnails": [
            {"url": "https://i.ytimg.com/small.jpg", "width": 480},
            {"url": "https://i.ytimg.com/full.jpg", "id": "maxresdefault"},
            {"url": "https://other.example/image.jpg", "width": 1920},
        ],
    }
    manager = ProcessManager()

    def run(command, **_kwargs):
        requests.append(command)
        return CompletedProcess(command, 0, "WARNING: example\n" + json.dumps(info))

    def thumbnail(urls, manager):
        assert urls == ["https://i.ytimg.com/full.jpg", "https://i.ytimg.com/small.jpg"]
        requests.append(urls)
        return b"test thumbnail"

    monkeypatch.setattr(manager, "run", run)
    monkeypatch.setattr(youtube_metadata, "_fetch_thumbnail", thumbnail)
    metadata, image = fetch_youtube_metadata(VIDEO, manager, cache_dir=tmp_path)
    assert metadata == YouTubeMetadata(VIDEO, "Sample video", "Sample channel", 5076, "1080 × 1920", 1234, "2024-06-01")
    assert image == b"test thumbnail"
    assert {"--skip-download", "--no-playlist", "--simulate", "--print"} <= set(requests[0])
    assert "--ffmpeg-location" not in requests[0]
    assert requests[0][-1] == youtube_watch_url(VIDEO)
    assert fetch_youtube_metadata(VIDEO, manager, cache_dir=tmp_path) == (metadata, image)
    assert len(requests) == 2


@pytest.mark.parametrize(("output", "message"), [
    ("ERROR: [youtube] Private video. Sign in to view.", "private"),
    ("ERROR: [youtube] This video is unavailable", "unavailable"),
    ("ERROR: [youtube] Failed to connect: timed out", "Check your connection"),
    ("ERROR: [youtube] Sign in to confirm you’re not a bot", "sign-in or verification"),
])
def test_extraction_errors_are_distinguishable(monkeypatch, tmp_path, output, message):
    manager = ProcessManager()

    def run(command, **_kwargs):
        raise CalledProcessError(1, command, output=output)

    monkeypatch.setattr(manager, "run", run)
    with pytest.raises(YouTubeMetadataError, match=message):
        fetch_youtube_metadata(VIDEO, manager, cache_dir=tmp_path)


def test_placeholder_metadata_does_not_look_like_success(monkeypatch, tmp_path):
    manager = ProcessManager()
    output = "WARNING: Video unavailable\n" + json.dumps({"id": VIDEO, "title": f"youtube video #{VIDEO}"})
    monkeypatch.setattr(manager, "run", lambda command, **_kwargs: CompletedProcess(command, 0, output))
    with pytest.raises(YouTubeMetadataError, match="unavailable"):
        fetch_youtube_metadata(VIDEO, manager, cache_dir=tmp_path)


def _wait_for(condition):
    until = monotonic() + 3
    while not condition() and monotonic() < until:
        QTest.qWait(20)
    assert condition()


def test_new_project_preview_debounce_stale_results_retry_and_creation(monkeypatch, tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    db = Database(tmp_path / "projects.db")
    monkeypatch.setattr("autoclip_studio.gui.Database", lambda: db)
    monkeypatch.setattr("autoclip_studio.gui.SettingsStore", lambda: SettingsStore(tmp_path / "settings.json"))
    monkeypatch.setattr(SettingsStore, "api_key", lambda *_args: "")
    release = Event()
    calls = []

    def fetch(video_id, manager):
        calls.append((video_id, manager))
        if video_id == VIDEO:
            release.wait(4)
        elif sum(key == OTHER for key, _ in calls) == 1:
            raise YouTubeMetadataError("Could not reach YouTube. Check your connection and retry.")
        title = "Obsolete video" if video_id == VIDEO else "New video title"
        return YouTubeMetadata(video_id, title, "Example channel", 5076, "3840 × 2160", 1234, "2024-06-01"), b""

    monkeypatch.setattr("autoclip_studio.ui.youtube_preview.fetch_youtube_metadata", fetch)
    window = MainWindow()
    window.resize(760, 560)
    window.show()
    window.show_page(1)
    view = window.youtube_preview
    submitted = []
    monkeypatch.setattr(window, "_start_worker", lambda project_id, options: submitted.append(options))
    try:
        window.source_input.setText("https://www.youtube.com/playlist?list=PL123")
        assert "Playlist links" in view.status.text()
        window.source_input.setText(youtube_watch_url(OTHER))
        QTest.qWait(250)
        window.source_input.setText(f"https://www.youtube.com/shorts/{VIDEO}")
        QTest.qWait(450)
        assert not calls
        _wait_for(lambda: bool(calls))
        assert view.skeleton.isVisible()
        window.source_input.setText(f"https://youtu.be/{OTHER}")
        assert calls[0][1].cancel_event.is_set()
        assert not view.isVisible()
        release.set()
        _wait_for(view.retry.isVisible)
        assert "Could not reach YouTube" in view.status.text()
        assert not window.project_name.text()
        view.retry.click()
        _wait_for(lambda: view.result is not None)
        assert "Video detected" in view.status.text()
        assert view.thumbnail.clock == "01:24:36"
        assert "1h 24m 36s" in view.stats.text()
        assert "1,234 views" in view.additional.text()
        assert window.project_name.text() == "New video title"
        assert not db.projects()
        window.project_name.setText("My custom project")
        window.source_input.setText(f"{youtube_watch_url(OTHER)}&list=PL123")
        assert len(calls) == 3
        window.generate()
        assert len(submitted) == 1
        assert submitted[0].project_name == "My custom project"
        assert submitted[0].youtube_metadata["video_id"] == OTHER
        assert json.loads(db.projects()[0]["options"])["project_name"] == "My custom project"
        window.source_input.setText(str(tmp_path / "local.mp4"))
        assert view.result is None and not view.isVisible()
        assert window.project_name.text() == "My custom project"
    finally:
        release.set()
        window.close()
        _wait_for(lambda: not window.isVisible())
        app.processEvents()
