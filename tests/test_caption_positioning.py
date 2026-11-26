from __future__ import annotations

from pathlib import Path

from autoclip_studio.captions import (
    DEFAULT_CAPTION_Y_RATIO,
    PLATFORM_SAFE_AREAS,
    TEMPLATES,
    FaceZone,
    estimate_caption_bbox,
    resolve_caption_y_center,
    write_ass,
)
from autoclip_studio.config import SettingsManager, SettingsStore
from autoclip_studio.models import Highlight, Segment, Transcript, Word
from autoclip_studio.process import ProcessManager
from autoclip_studio.rendering import render_clip
from autoclip_studio.smart_reframe import CropKeyframe, FaceDetection, FramingPlan, FramingSegment, OutputFaceWindow, _FrameSample, _compute_output_face_windows


def _style_margin_v(ass_text: str) -> int:
    for line in ass_text.splitlines():
        if line.startswith("Style: Default,"):
            return int(line.split(",")[-2])
    raise AssertionError("Missing Style: Default")


def test_default_vertical_placement_anchors_around_73_percent_across_resolutions(tmp_path: Path) -> None:
    tr = Transcript(4.0, "id", [Segment(0.2, 2.0, "Halo semuanya!")])
    hl = Highlight(0.0, 4.0, "DefaultY", "", "", 85)
    for res in ((1080, 1920), (720, 1280)):
        out = tmp_path / f"cap_{res[0]}x{res[1]}.ass"
        write_ass(tr, hl, out, res, "Impact")
        mv = _style_margin_v(out.read_text(encoding="utf-8-sig"))
        _, bh = estimate_caption_bbox("Halo semuanya!", TEMPLATES["Impact"], *res)
        # MarginV is bottom distance from frame height; implied center = height - mv - bh*height*0.5
        center_ratio = (res[1] - mv - (bh * res[1]) * 0.5) / res[1]
        assert abs(center_ratio - DEFAULT_CAPTION_Y_RATIO) <= 0.025


def test_multiline_and_bounce_animation_scale_preserve_face_clearance(tmp_path: Path) -> None:
    single_w, single_h = estimate_caption_bbox("Short line", TEMPLATES["Impact"], 1080, 1920)
    two_line_w, two_line_h = estimate_caption_bbox("Ini adalah kalimat panjang dua baris buat uji coba", TEMPLATES["Impact"], 1080, 1920)
    assert two_line_h > single_h * 1.45

    # Pop Bounce peaks at 1.22x scale so effective box exceeds base font scale
    bounce_w, bounce_h = estimate_caption_bbox("BOUNCE!", TEMPLATES["Pop Bounce"], 1080, 1920)
    clean_w, clean_h = estimate_caption_bbox("BOUNCE!", TEMPLATES["Clean"], 1080, 1920)
    assert bounce_h > clean_h and bounce_w > clean_w

    # Face sitting down to y=0.69 collides with 2-line Impact at default 0.73 -> shifts lower below chin
    two_line_text = "Ini kalimat dua baris buat tes"
    _, two_h = estimate_caption_bbox(two_line_text, TEMPLATES["Impact"], 1080, 1920)
    assert two_h > single_h * 1.45
    face = (FaceZone(0.0, 3.0, ((0.30, 0.35, 0.70, 0.69),)),)
    cy, shifted = resolve_caption_y_center(
        0.0, 2.5, two_line_text, TEMPLATES["Impact"],
        1080, 1920, y_ratio=0.73, avoid_faces=True, face_regions=face,
    )
    assert shifted
    assert cy > 0.73


def test_face_aware_placement_preserves_preferred_y_when_clear_and_uses_hysteresis(tmp_path: Path) -> None:
    # Speaker face high in frame (0.20..0.46) does NOT collide with 0.73 caption -> preserved strictly!
    high_face = (FaceZone(0.0, 5.0, ((0.32, 0.20, 0.68, 0.46),)),)
    cy_clear, shifted_clear = resolve_caption_y_center(
        0.2, 2.0, "Aman di dada bawah", TEMPLATES["Impact"], 1080, 1920,
        y_ratio=0.73, avoid_faces=True, face_regions=high_face,
    )
    assert not shifted_clear
    assert abs(cy_clear - 0.73) < 1e-4

    # When avoid_faces=False, colliding preference is still respected untouched
    cy_manual, shifted_manual = resolve_caption_y_center(
        0.2, 2.0, "Manual override", TEMPLATES["Impact"], 1080, 1920,
        y_ratio=0.35, avoid_faces=False, face_regions=high_face,
    )
    assert not shifted_manual
    assert abs(cy_manual - 0.35) < 1e-4

    # Nearby settled position reuses prev_y via hysteresis to prevent unnecessary bobbing
    cy_next, _ = resolve_caption_y_center(
        2.1, 4.0, "Tetap stabil", TEMPLATES["Impact"], 1080, 1920,
        y_ratio=0.73, avoid_faces=True, face_regions=high_face, prev_y=0.75,
    )
    assert abs(cy_next - 0.75) < 1e-4


def test_aspect_ratios_and_split_screen_face_projection() -> None:
    # Landscape (1920x1080) and Square (1080x1080) stay strictly within frame
    for w, h in ((1920, 1080), (1080, 1080), (1080, 1920)):
        cy, _ = resolve_caption_y_center(0.0, 2.0, "Testing layout bounds", TEMPLATES["Clean"], w, h)
        _, bh = estimate_caption_bbox("Testing layout bounds", TEMPLATES["Clean"], w, h)
        assert cy - bh * 0.5 >= 0.02 and cy + bh * 0.5 <= 0.98

    # Map source faces across both stacked split_screen panels into output normalized coordinates
    left_det = FaceDetection(300, 220, 160, 180)
    right_det = FaceDetection(1400, 260, 160, 180)
    samples = [_FrameSample(t * 0.5, False, 0.8, [(1, left_det, 0.4), (2, right_det, 0.3)], 960.0) for t in range(6)]
    split_seg = FramingSegment("split", 0.0, 3.0, top_box=(180, 120, 420, 400), bottom_box=(1280, 160, 420, 400))
    wins = _compute_output_face_windows(samples, [split_seg], 1920, 1080, 608, (1080, 1920))
    assert len(wins) == 1
    assert len(wins[0].boxes) == 2
    ys = sorted(box[1] for box in wins[0].boxes)
    assert ys[0] < 0.45 and ys[1] >= 0.50


def test_safe_areas_never_leak_into_ass_and_render_clip_forwards_face_regions(tmp_path: Path, monkeypatch) -> None:
    assert {"all", "tiktok", "reels", "shorts"}.issubset(PLATFORM_SAFE_AREAS.keys())
    captured: dict = {}
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"0" * 4096)
    mgr = ProcessManager()

    fake_plan = FramingPlan(
        segments=[FramingSegment("single", 0.0, 4.0, [CropKeyframe(0.0, 100.0)])],
        face_windows=[OutputFaceWindow(0.0, 4.0, [(0.3, 0.2, 0.7, 0.48)])],
    )
    monkeypatch.setattr("autoclip_studio.rendering.probe", lambda *_a: {
        "streams": [{"codec_type": "video", "width": 1920, "height": 1080, "avg_frame_rate": "25/1", "r_frame_rate": "25/1"}],
        "format": {"duration": "8.0"},
    })
    monkeypatch.setattr("autoclip_studio.rendering.analyze_smart_reframe", lambda *_a, **_kw: fake_plan)
    monkeypatch.setattr("autoclip_studio.rendering.find_media_tool", lambda n: n)
    monkeypatch.setattr("autoclip_studio.rendering.validate_mp4", lambda *_a: 4.0)
    monkeypatch.setattr("autoclip_studio.rendering.write_ass", lambda *_a, **kw: captured.update(kw))
    monkeypatch.setattr(mgr, "run", lambda cmd, **_kw: Path(cmd[-1]).write_bytes(b"mp4"))

    render_clip(
        src, Highlight(0.0, 4.0, "T", "", "", 80), Transcript(4.0, "id", [Segment(0.2, 3.5, "Halo")]),
        tmp_path / "out.mp4", tmp_path / "work", (1080, 1920), "9:16",
        True, True, "Impact", False, mgr, lambda _m: None,
        caption_y_ratio=0.68, caption_avoid_faces=True,
    )
    assert captured["y_ratio"] == 0.68
    assert captured["avoid_faces"] is True
    assert len(captured["face_regions"]) == 1


def test_caption_positioning_ui_controls_persist_and_reset(tmp_path: Path, monkeypatch) -> None:
    from PySide6.QtWidgets import QApplication, QMessageBox
    from autoclip_studio.database import Database
    from autoclip_studio.gui import MainWindow

    app = QApplication.instance() or QApplication([])
    assert app is not None
    settings_path = tmp_path / "settings.json"
    store = SettingsManager(settings_path)
    monkeypatch.setattr(store, "api_key", lambda *_a: "")
    monkeypatch.setattr(store, "set_api_key", lambda *_a: None)
    monkeypatch.setattr("autoclip_studio.gui.SettingsStore", lambda: store)
    monkeypatch.setattr("autoclip_studio.gui.Database", lambda: Database(tmp_path / "app.db"))

    win = MainWindow()
    try:
        assert store.caption_positioning() == (0.73, True, True, "all")
        win.caption_library.y_slider.setValue(64)
        win.caption_library.avoid_faces_check.setChecked(False)
        win.caption_library.safe_platform_combo.setCurrentIndex(win.caption_library.safe_platform_combo.findData("tiktok"))
        win.caption_library.safe_area_check.setChecked(False)
        assert store.caption_positioning() == (0.64, False, False, "tiktok")
        assert not win.caption_library.safe_platform_combo.isEnabled()

        win.caption_library.reset_positioning_defaults()
        assert store.caption_positioning() == (0.73, True, True, "all")

        win.caption_library.y_slider.setValue(79)
    finally:
        win.close()

    reloaded = SettingsStore(settings_path)
    assert reloaded.caption_positioning()[0] == 0.79

    monkeypatch.setattr("autoclip_studio.gui.SettingsStore", lambda: reloaded)
    monkeypatch.setattr(QMessageBox, "question", lambda *_a, **_kw: QMessageBox.StandardButton.Yes)
    win2 = MainWindow()
    try:
        assert round(win2.caption_library.positioning_preferences()[0], 2) == 0.79
        win2.reset_all_settings()
        assert reloaded.caption_positioning() == (0.73, True, True, "all")
    finally:
        win2.close()
