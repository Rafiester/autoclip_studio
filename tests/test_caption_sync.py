from __future__ import annotations

from pathlib import Path

from autoclip_studio.captions import TEMPLATES, build_caption_cues, write_ass
from autoclip_studio.config import SettingsStore
from autoclip_studio.models import Highlight, Segment, Transcript, Word
from autoclip_studio.process import ProcessManager
from autoclip_studio.rendering import _build_filter_args, render_clip
from autoclip_studio.smart_reframe import FramingPlan, FramingSegment


def _parse_ass_events(ass_text: str) -> list[tuple[int, float, float, str]]:
    def to_sec(ts: str) -> float:
        h, m, s = ts.split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)

    out: list[tuple[int, float, float, str]] = []
    for line in ass_text.splitlines():
        if not line.startswith("Dialogue:"):
            continue
        parts = line.split(",", 9)
        layer = int(parts[0].split(":")[1].strip())
        out.append((layer, to_sec(parts[1]), to_sec(parts[2]), parts[9]))
    return out


def test_word_normalization_repairs_isolated_zeros_and_merges_indonesian_hyphens(tmp_path: Path) -> None:
    words = [
        Word(1.00, 1.30, "harus"),
        Word(1.30, 1.30, "bener"),      # isolated zero-duration Whisper token (1 of 7)
        Word(1.55, 1.85, "-bener"),
        Word(1.90, 2.25, "sembuh"),
        Word(2.35, 2.55, "buat"),
        Word(2.60, 2.75, "laki"),
        Word(2.75, 2.75, "-"),
        Word(2.80, 3.10, "laki."),
    ]
    tr = Transcript(5.0, "id", [Segment(1.0, 3.10, "harus bener -bener sembuh buat laki - laki.", words)])
    hl = Highlight(0.0, 5.0, "Health", "", "", 90)
    target = tmp_path / "hyphen.ass"
    write_ass(tr, hl, target, (1080, 1920), "Hormozi")
    content = target.read_text(encoding="utf-8-sig")
    assert "BENER-BENER" in content
    assert "LAKI-LAKI." in content
    assert " -BENER" not in content
    assert " - " not in content
    # Confirms word timing stayed intact (multiple word-level highlight events, not collapsed fallback).
    events = _parse_ass_events(content)
    assert len(events) >= 4
    assert all(events[i][1] >= events[i - 1][2] - 1e-6 for i in range(1, len(events)))


def test_boundary_bleed_only_segment_does_not_fall_back_to_offclip_text(tmp_path: Path) -> None:
    # Mirrors reference segment 838 where only 20ms of the previous sentence crossed highlight.start.
    prior_words = [
        Word(9.00, 9.40, "Dulu"),
        Word(9.40, 9.70, "pas"),
        Word(9.70, 9.98, "SMA"),
        Word(9.98, 10.52, "sehari,"),  # 20ms crosses highlight.start = 10.50
    ]
    in_clip_words = [
        Word(10.52, 10.70, "ini"),
        Word(10.70, 11.08, "dulu"),
        Word(11.08, 11.26, "ya."),
    ]
    tr = Transcript(20.0, "id", [
        Segment(9.00, 10.52, "Dulu pas SMA sehari,", prior_words),
        Segment(10.52, 11.26, "ini dulu ya.", in_clip_words),
    ])
    hl = Highlight(10.50, 15.00, "Story", "", "", 85)
    target = tmp_path / "bleed.ass"
    write_ass(tr, hl, target, (1080, 1920), "Impact", fps=25.0)
    content = target.read_text(encoding="utf-8-sig")
    assert "DULU PAS SMA" not in content
    assert "SEHARI," not in content
    events = _parse_ass_events(content)
    assert len(events) == 1
    assert "INI DULU YA." in events[0][3]
    assert events[0][2] - events[0][1] >= 0.40


def test_balanced_clause_grouping_eliminates_orphans_and_splits_on_pauses() -> None:
    words = [
        Word(0.00, 0.25, "kalau"),
        Word(0.25, 0.50, "misal"),
        Word(0.50, 0.72, "kalian"),
        Word(0.72, 0.94, "main"),
        Word(0.94, 1.15, "futsal"),
        Word(1.16, 1.34, "dulu."),  # 6th word in Impact (max_words=5) -> must rebalance 4+2
        Word(1.90, 2.20, "baru"),   # 0.56s pause before "baru" -> must break
        Word(2.20, 2.60, "mandi."),
    ]
    tr = Transcript(4.0, "id", [Segment(0.0, 2.60, "kalau misal kalian main futsal dulu. baru mandi.", words)])
    hl = Highlight(0.0, 4.0, "Clause", "", "", 80)
    cues = build_caption_cues(tr, hl, TEMPLATES["Impact"])
    texts = [cue.text for cue in cues]
    assert texts == ["kalau misal kalian main", "futsal dulu.", "baru mandi."]


def test_normalize_timeline_eliminates_overlaps_and_quantizes_fps(tmp_path: Path) -> None:
    tr = Transcript(6.0, "id", [
        Segment(0.10, 1.25, "Kalimat pertama agak panjang."),
        Segment(1.10, 2.00, "Kalimat kedua tumpang tindih."),
        Segment(2.02, 2.16, "Singkat."),  # 20ms micro-gap & 140ms short burst
    ])
    hl = Highlight(0.0, 6.0, "Norm", "", "", 75)
    target = tmp_path / "quantized.ass"
    write_ass(tr, hl, target, (1080, 1920), "Impact", fps=25.0)
    events = _parse_ass_events(target.read_text(encoding="utf-8-sig"))
    assert len(events) == 3
    for idx, (_, start, end, _) in enumerate(events):
        assert end > start
        assert round(start * 25) - start * 25 < 1e-6 or abs(round(start * 25) - start * 25) < 1e-4
        assert abs(round(end * 25) - end * 25) < 1e-4
        if idx > 0:
            assert start >= events[idx - 1][2] - 1e-6
    # Micro-gap (1.98 -> 2.02) closed seamlessly & short final cue extended >= 0.36s inside trailing silence.
    assert abs(events[2][1] - events[1][2]) <= 0.001
    assert events[2][2] - events[2][1] >= 0.36


def test_sync_modes_and_offset_ms_shift_and_clamp() -> None:
    words = [Word(1.00, 1.35, "Cepat"), Word(1.36, 1.70, "sekali")]
    tr = Transcript(4.0, "id", [Segment(1.00, 1.70, "Cepat sekali", words)])
    hl = Highlight(0.0, 4.0, "Sync", "", "", 70)

    precise = build_caption_cues(tr, hl, TEMPLATES["Impact"], sync_mode="precise", offset_ms=0)
    readable = build_caption_cues(tr, hl, TEMPLATES["Impact"], sync_mode="readable", offset_ms=0)
    shifted = build_caption_cues(tr, hl, TEMPLATES["Impact"], sync_mode="precise", offset_ms=120)

    assert abs(precise[0].start - 1.00) < 1e-6
    assert readable[0].start < precise[0].start
    assert readable[0].end >= precise[0].end
    assert abs(shifted[0].start - 1.12) < 1e-6


def test_animated_templates_stability_and_split_screen_viral_anchor(tmp_path: Path) -> None:
    words = [
        Word(0.20, 0.50, "First"),
        Word(0.55, 0.90, "second"),  # 50ms gap <= 140ms should bridge without blank flicker
        Word(0.95, 1.35, "third"),
    ]
    tr = Transcript(3.0, "en", [Segment(0.20, 1.35, "First second third", words)])
    hl = Highlight(0.0, 3.0, "Anim", "", "", 80)

    # Viral Shorts split_screen uses vertical center anchor (height // 2 = 960) and single entrance \move.
    viral_path = tmp_path / "viral.ass"
    write_ass(tr, hl, viral_path, (1080, 1920), "Viral Shorts", split_screen=True)
    viral_text = viral_path.read_text(encoding="utf-8-sig")
    assert viral_text.count(r"\move(") == 1
    assert ",960,0," in viral_text

    # Box Highlight bridges 50ms inter-word gaps without emitting blank neutral lines between active words.
    box_path = tmp_path / "box.ass"
    write_ass(tr, hl, box_path, (1080, 1920), "Box Highlight")
    box_events = _parse_ass_events(box_path.read_text(encoding="utf-8-sig"))
    assert len(box_events) == 3

    # Typewriter maintains stable phrase font sizing throughout typing regardless of trailing long word.
    long_tr = Transcript(3.0, "en", [Segment(0.20, 1.20, "Go supercalifragilistic", [
        Word(0.20, 0.50, "Go"),
        Word(0.55, 1.20, "supercalifragilistic"),
    ])])
    type_path = tmp_path / "type.ass"
    write_ass(long_tr, hl, type_path, (1080, 1920), "Typewriter")
    type_events = _parse_ass_events(type_path.read_text(encoding="utf-8-sig"))
    fs_sizes = {line.split("}", 1)[0] for _, _, _, line in type_events if r"\fs" in line}
    assert len(fs_sizes) == 1

    # Impact scales punch animation inside ultra-short constrained cues (<260ms) when adjacent speech prevents extension.
    short_tr = Transcript(2.0, "en", [Segment(0.50, 0.68, "Go!"), Segment(0.68, 1.60, "Next")])
    short_impact = tmp_path / "short_impact.ass"
    write_ass(short_tr, Highlight(0.0, 2.0, "", "", "", 50), short_impact, (1080, 1920), "Impact")
    short_events = _parse_ass_events(short_impact.read_text(encoding="utf-8-sig"))
    assert "GO!" in short_events[0][3]
    assert r"\t(0,105," not in short_events[0][3]


def test_filter_graph_and_render_clip_timebase_forwarding(tmp_path: Path, monkeypatch) -> None:
    single = FramingPlan(segments=[FramingSegment("single", 0.0, 5.0)])
    fit = FramingPlan(segments=[FramingSegment("fit", 0.0, 5.0)])
    split = FramingPlan(segments=[FramingSegment("split", 0.0, 5.0, top_box=(0, 0, 500, 500), bottom_box=(500, 0, 500, 500))])
    mixed = FramingPlan(segments=[
        FramingSegment("split", 0.0, 2.5, top_box=(0, 0, 500, 500), bottom_box=(500, 0, 500, 500)),
        FramingSegment("single", 2.5, 5.0),
    ])

    for plan, _label in ((single, "single"), (fit, "fit"), (split, "split"), (mixed, "mixed")):
        args, _ = _build_filter_args(plan, 606, 1080, 0, 657.0, (1080, 1920), captions=True)
        assert "setpts=PTS-STARTPTS" in args[1]
        vfr_args, _ = _build_filter_args(plan, 606, 1080, 0, 657.0, (1080, 1920), captions=True, vfr_fps=29.97)
        assert "fps=29.97:round=near,setpts=PTS-STARTPTS" in vfr_args[1]

    captured: dict = {}
    commands: list[list[str]] = []
    src = tmp_path / "src.mp4"
    src.write_bytes(b"0" * 4096)
    dst = tmp_path / "out.mp4"
    mgr = ProcessManager()

    monkeypatch.setattr("autoclip_studio.rendering.probe", lambda *_a: {
        "streams": [{"codec_type": "video", "width": 1920, "height": 1080, "avg_frame_rate": "25/1", "r_frame_rate": "25/1"},
                    {"codec_type": "audio", "codec_name": "aac"}],
        "format": {"duration": "10.0"},
    })
    monkeypatch.setattr("autoclip_studio.rendering.find_media_tool", lambda name: name)
    monkeypatch.setattr("autoclip_studio.rendering.validate_mp4", lambda *_a: 5.0)
    monkeypatch.setattr("autoclip_studio.rendering.write_ass", lambda *_a, **kw: captured.update(kw))

    def fake_run(cmd, **_kw):
        commands.append([str(part) for part in cmd])
        Path(cmd[-1]).write_bytes(b"rendered-mp4")

    monkeypatch.setattr(mgr, "run", fake_run)
    dur = render_clip(
        src, Highlight(1.0, 6.0, "Clip", "", "", 85), Transcript(10.0, "id", [Segment(1.0, 5.0, "Tes.")]),
        dst, tmp_path / "work", (1080, 1920), "9:16", False, True, "Impact", False, mgr, lambda _m: None,
        caption_sync_mode="readable", caption_offset_ms=60,
    )
    assert dur == 5.0
    assert captured["fps"] == 25.0
    assert captured["sync_mode"] == "readable"
    assert captured["offset_ms"] == 60
    assert any("-af" in cmd and "asetpts=PTS-STARTPTS" in cmd for cmd in commands)


def test_caption_timing_preferences_persist_and_reset(tmp_path: Path, monkeypatch) -> None:
    from PySide6.QtWidgets import QApplication, QMessageBox
    from autoclip_studio.database import Database
    from autoclip_studio.gui import MainWindow

    app = QApplication.instance() or QApplication([])
    assert app is not None
    settings_path = tmp_path / "settings.json"
    db_path = tmp_path / "app.db"
    store = SettingsStore(settings_path)
    monkeypatch.setattr(store, "api_key", lambda *_a: "")
    monkeypatch.setattr(store, "set_api_key", lambda *_a: None)
    monkeypatch.setattr("autoclip_studio.gui.SettingsStore", lambda: store)
    monkeypatch.setattr("autoclip_studio.gui.Database", lambda: Database(db_path))

    win = MainWindow()
    try:
        assert store.caption_timing() == ("precise", 0)
        idx = win.caption_library.sync_mode.findData("readable")
        win.caption_library.sync_mode.setCurrentIndex(idx)
        win.caption_library.offset_spin.setValue(60)
        assert store.caption_timing() == ("readable", 60)
    finally:
        win.close()

    reloaded = SettingsStore(settings_path)
    assert reloaded.caption_timing() == ("readable", 60)

    monkeypatch.setattr("autoclip_studio.gui.SettingsStore", lambda: reloaded)
    monkeypatch.setattr(QMessageBox, "question", lambda *_a, **_kw: QMessageBox.StandardButton.Yes)
    win2 = MainWindow()
    try:
        assert win2.caption_library.timing_preferences() == ("readable", 60)
        win2.reset_all_settings()
        assert reloaded.caption_timing() == ("precise", 0)
        assert win2.caption_library.timing_preferences() == ("precise", 0)
    finally:
        win2.close()
