import subprocess
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from autoclip_studio import media, rendering, transcription
from autoclip_studio.highlights import parse_highlights, select_highlights
from autoclip_studio.models import Highlight, Segment, Transcript
from autoclip_studio.process import CancelledError, ProcessManager
from autoclip_studio.providers import AIProvider


def test_provider_array_and_invalid_entries():
    raw = '[null, true, {"start": NaN, "end": 20}, {"start": 2, "end": 10}]'
    array = parse_highlights(raw, 30)
    assert array == parse_highlights('{"highlights":' + raw + '}', 30)
    assert [(clip.start, clip.end) for clip in array] == [(2, 10)]
    assert parse_highlights('{"highlights": null}', 30) == []


def test_cancellation_stops_provider_chunks():
    manager = ProcessManager()
    prompts = []

    def complete(system, prompt):
        prompts.append(prompt)
        manager.cancel()
        return '{"highlights": []}'

    provider = SimpleNamespace(kind="custom", complete=complete)
    transcript = Transcript(2400, "en", [Segment(10, 20, "First."), Segment(1500, 1510, "Second.")])
    with pytest.raises(CancelledError):
        select_highlights(transcript, 1, 10, 30, provider, lambda _message: None, manager=manager)
    assert len(prompts) == 1


def test_gemini_poll_cancellation_deletes_uploaded_file(monkeypatch, tmp_path):
    video = tmp_path / "sample.mp4"
    video.write_bytes(b"sample")
    event = Event()
    deleted = []

    def post(url, **kwargs):
        if "data" not in kwargs:
            return SimpleNamespace(headers={"X-Goog-Upload-URL": "https://upload.invalid"}, status_code=200)
        assert kwargs["data"].read() == b"sample"
        event.set()
        return SimpleNamespace(
            json=lambda: {"file": {"name": "files/example", "state": "PROCESSING"}},
            status_code=200,
        )

    monkeypatch.setattr("autoclip_studio.providers.requests.post", post)
    monkeypatch.setattr("autoclip_studio.providers.requests.delete", lambda url, **_kwargs: deleted.append(url))
    provider = AIProvider("gemini", "mock-key", "https://provider.invalid/v1beta", "model")
    with pytest.raises(CancelledError):
        provider.complete_video(video, "Find a highlight", cancel_event=event)
    assert deleted == ["https://provider.invalid/v1beta/files/example"]


def test_download_identity_and_uncached_job_isolation(monkeypatch, tmp_path):
    monkeypatch.setattr(media, "CACHE_DIR", tmp_path / "shared")
    monkeypatch.setattr(media, "find_media_tool", lambda name: str(tmp_path / f"{name}.exe"))
    monkeypatch.setattr(media, "bundled_binary", lambda _name: None)
    manager = ProcessManager()
    downloads = []

    def run(command, **_kwargs):
        result = Path(command[command.index("-o") + 1].replace("%(ext)s", "mp4"))
        receipt = Path(command[command.index("--print-to-file") + 2])
        result.write_bytes(b"video")
        receipt.write_text(str(result) + "\n", encoding="utf-8")
        downloads.append(command[-1])

    monkeypatch.setattr(manager, "run", run)
    first = "https://www.youtube.com/@first/live"
    second = "https://www.youtube.com/@second/live"
    cached_first = media.acquire_source(first, True, manager)
    cached_second = media.acquire_source(second, True, manager)
    assert cached_first != cached_second
    assert media.acquire_source(first, True, manager) == cached_first
    uncached = media.acquire_source(first, False, manager, job_dir=tmp_path / "job")
    assert uncached.parent == tmp_path / "job" / "download"
    assert downloads == [first, second, first]


def test_transcript_cache_separates_language(monkeypatch, tmp_path):
    monkeypatch.setattr(transcription, "CACHE_DIR", tmp_path)
    source = tmp_path / "source.mp4"
    source.touch()
    key = transcription.transcript_cache_path
    assert len({key(source, "tiny"), key(source, "tiny", "en"), key(source, "tiny", "id")}) == 3


@pytest.mark.parametrize("validator_fails", [False, True])
def test_render_falls_back_and_publishes_only_valid_output(monkeypatch, tmp_path, validator_fails):
    final = tmp_path / "clip.mp4"
    final.write_bytes(b"previous export")
    commands = []
    manager = ProcessManager()

    def run(command, **_kwargs):
        codec = command[command.index("-c:v") + 1]
        commands.append(codec)
        staged = Path(command[-1])
        assert staged != final
        staged.write_bytes(codec.encode())
        if codec == "h264_nvenc":
            raise subprocess.CalledProcessError(1, command, output="Simulated encoder failure")

    def validate(path, _manager):
        assert path.read_bytes() == b"libx264"
        if validator_fails:
            raise RuntimeError("Simulated validation failure")
        return 6.0

    monkeypatch.setattr(manager, "run", run)
    monkeypatch.setattr(rendering, "find_media_tool", lambda _name: "ffmpeg")
    monkeypatch.setattr(rendering, "nvenc_available", lambda _manager: True)
    monkeypatch.setattr(rendering, "validate_mp4", validate)
    args = (
        tmp_path / "source.mp4", Highlight(0, 6, "Title", "", "", 70), Transcript(6, "en", []),
        final, tmp_path / "work", (360, 640), "9:16", False, False,
        "Impact", True, manager, lambda _message: None,
    )
    if validator_fails:
        with pytest.raises(RuntimeError, match="validation failure"):
            rendering.render_clip(*args, dimensions=(1280, 720))
        assert final.read_bytes() == b"previous export"
    else:
        assert rendering.render_clip(*args, dimensions=(1280, 720)) == 6
        assert final.read_bytes() == b"libx264"
    assert commands == ["h264_nvenc", "libx264"]
    assert list(tmp_path.glob("*.mp4")) == [final]
