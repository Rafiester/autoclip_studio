from __future__ import annotations

import os
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from autoclip_studio.captions import write_ass
from autoclip_studio.config import SettingsStore
from autoclip_studio.database import Database
from autoclip_studio.models import Highlight, ProjectOptions, Segment, Transcript
from autoclip_studio.pipeline import Pipeline
from autoclip_studio.process import ProcessManager, find_media_tool
from autoclip_studio.subtitles import SrtValidationError, parse_srt_file


def _app() -> QApplication:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QApplication.instance() or QApplication([])


def _make_video(path: Path, duration: int = 8) -> None:
    ProcessManager().run([
        find_media_tool("ffmpeg"), "-hide_banner", "-y",
        "-f", "lavfi", "-i", f"testsrc2=size=640x360:rate=15:duration={duration}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", path,
    ])


def test_srt_utf8_bom_indonesian_multiline_and_ass_captions(tmp_path: Path) -> None:
    srt_path = tmp_path / "indo.srt"
    srt_text = (
        "1\n"
        "00:00:01,000 --> 00:00:04,500\n"
        "Ini adalah contoh\n"
        "subtitle bahasa Indonesia.\n\n"
        "2\n"
        "00:00:05,000 --> 00:00:09,000\n"
        "Kita bisa membuat klip\n"
        "dengan cepat dan mudah.\n"
    )
    srt_path.write_bytes(b"\xef\xbb\xbf" + srt_text.encode("utf-8"))

    res = parse_srt_file(srt_path, offset_seconds=0.5, fallback_duration=10.0)
    assert res.encoding == "UTF-8 BOM"
    assert res.detected_language == "id"
    assert res.cue_count == 2
    assert res.transcript.segments[0].words == []
    assert abs(res.transcript.segments[0].start - 1.5) < 0.01
    assert "\n" in res.transcript.segments[0].text

    ass_out = tmp_path / "out.ass"
    write_ass(
        res.transcript,
        Highlight(2.0, 8.0, "Klip", "Hook", "Desc", 90),
        ass_out, (1080, 1920), "Clean",
    )
    ass_content = ass_out.read_text(encoding="utf-8-sig")
    assert r"subtitle bahasa Indonesia." in ass_content
    assert r"\N" in ass_content
    assert "0:00:00.00,0:00:03.00" in ass_content  # cue 1 clamped to [2.0, 5.0] -> [0, 3.0]


def test_srt_validation_errors(tmp_path: Path) -> None:
    empty = tmp_path / "empty.srt"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(SrtValidationError, match="empty"):
        parse_srt_file(empty)

    bad = tmp_path / "bad.srt"
    bad.write_text("not an srt block", encoding="utf-8")
    with pytest.raises(SrtValidationError, match="Malformed"):
        parse_srt_file(bad)

    txt = tmp_path / "notes.txt"
    txt.write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n", encoding="utf-8")
    with pytest.raises(SrtValidationError, match="valid .srt"):
        parse_srt_file(txt)


@pytest.mark.parametrize(
    "source_kind,use_srt",
    [
        ("youtube", False),
        ("youtube", True),
        ("local", False),
        ("local", True),
    ],
)
def test_pipeline_all_four_workflows(tmp_path: Path, monkeypatch, source_kind: str, use_srt: bool) -> None:
    video = tmp_path / "local_source.mp4"
    _make_video(video, duration=8)
    video_mtime = video.stat().st_mtime_ns

    srt = tmp_path / "subs.srt"
    srt.write_text(
        "1\n00:00:00,500 --> 00:00:03,500\nIni penting untuk diketahui.\n\n"
        "2\n00:00:04,000 --> 00:00:07,500\nJadi jangan lewatkan tips ini.\n",
        encoding="utf-8",
    )
    srt_mtime = srt.stat().st_mtime_ns

    monkeypatch.setattr("autoclip_studio.pipeline.CACHE_DIR", tmp_path / "cache")
    db = Database(tmp_path / "db.sqlite")
    settings = SettingsStore(tmp_path / "settings.json")
    settings.data["hardware_encoding"] = False

    calls = {"download": 0, "transcribe": 0}

    def fake_acquire(src, *_args, **_kwargs):
        if source_kind == "youtube":
            calls["download"] += 1
        return video

    def fake_transcribe(*_args, **_kwargs):
        calls["transcribe"] += 1
        return Transcript(8.0, "en", [Segment(0.0, 7.5, "Transcribed speech text", [])])

    monkeypatch.setattr("autoclip_studio.pipeline.acquire_source", fake_acquire)
    monkeypatch.setattr("autoclip_studio.pipeline.transcribe", fake_transcribe)
    monkeypatch.setattr(
        "autoclip_studio.pipeline.select_highlights",
        lambda *_a, **_kw: [Highlight(0.5, 6.5, "Clip 1", "Hook", "Desc", 88)],
    )

    source_val = "https://www.youtube.com/watch?v=dQw4w9WgXcQ" if source_kind == "youtube" else str(video)
    options = ProjectOptions(
        source=source_val,
        clip_count=1, min_duration=3, max_duration=10,
        resolution="360x640", aspect_ratio="9:16",
        face_tracking=False, captions=True, caption_template="Clean",
        output_dir=str(tmp_path / "out"),
        srt_path=str(srt) if use_srt else "",
    )
    pid = db.create_project(source_val, {})
    statuses: list[str] = []
    clips = Pipeline(db, settings).run(pid, options, lambda _p, msg: statuses.append(msg))

    assert len(clips) == 1
    assert clips[0].path.is_file()
    assert calls["download"] == (1 if source_kind == "youtube" else 0)
    assert calls["transcribe"] == (0 if use_srt else 1)
    assert video.stat().st_mtime_ns == video_mtime
    assert srt.stat().st_mtime_ns == srt_mtime
    if source_kind == "local":
        assert any("download skipped" in s.lower() for s in statuses)
    if use_srt:
        assert any("transcription skipped" in s.lower() for s in statuses)


def test_new_project_ui_tabs_and_srt_import(tmp_path: Path, monkeypatch) -> None:
    app = _app()
    from autoclip_studio.gui import MainWindow

    monkeypatch.setattr("autoclip_studio.gui.Database", lambda: Database(tmp_path / "ui.db"))
    video = tmp_path / "demo.mp4"
    _make_video(video, duration=4)
    srt = tmp_path / "demo_id.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:03,000\nHalo dunia, ini sangat bagus!\n", encoding="utf-8")

    window = MainWindow()
    try:
        window.show()
        window.show_page(1)
        assert window.youtube_tab_btn.isChecked()
        assert not window.local_tab_btn.isChecked()
        assert window.source_stack.currentIndex() == 0
        assert not window.import_srt_check.isChecked()
        assert not window.srt_panel.isVisible()

        # Existing lower controls preserved
        assert window.clip_count.value() == 3
        assert window.min_duration.value() == 25
        assert window.max_duration.value() == 60
        assert window.generate_button.isEnabled()

        # Switch to Local File tab & import SRT
        window.set_source_tab(1)
        assert window.source_stack.currentIndex() == 1
        window._set_local_source(str(video))
        window.import_srt_check.setChecked(True)
        window.srt_input.setText(str(srt))
        app.processEvents()

        assert window.srt_panel.isVisible()
        assert "Valid SRT" in window.srt_status.text()
        assert "Indonesian" in window.srt_status.text()
    finally:
        window.close()
