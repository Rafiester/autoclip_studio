from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtWidgets import QApplication

from autoclip_studio.captions import write_ass
from autoclip_studio.database import Database
from autoclip_studio.models import Highlight, Segment, Transcript
from autoclip_studio.process import ProcessManager, find_media_tool
from autoclip_studio.rendering import _build_filter_args, render_clip
from autoclip_studio.smart_reframe import (
    CropKeyframe,
    FaceDetection,
    FramingMetrics,
    FramingPlan,
    FramingSegment,
    _FrameSample,
    _build_scene_adaptive_plan,
    _build_single_track_keyframes,
    can_group_frame,
    compute_split_box,
    keyframes_to_crop_x_expression,
)


def _app() -> QApplication:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QApplication.instance() or QApplication([])


def test_group_feasibility_never_averages_distant_podcast_faces() -> None:
    crop_w = 608  # 9:16 crop inside 1920x1080
    left_host = FaceDetection(320, 260, 140, 160)
    right_host = FaceDetection(1420, 260, 140, 160)
    fits, _ = can_group_frame([left_host, right_host], crop_w)
    assert not fits

    # Close faces fit safely inside 9:16 crop
    close_a = FaceDetection(760, 260, 120, 140)
    close_b = FaceDetection(950, 260, 120, 140)
    fits_close, cx = can_group_frame([close_a, close_b], crop_w)
    assert fits_close
    assert 850 <= cx <= 980


def test_active_speaker_avoids_midpoint_handles_hysteresis_dropouts_and_cuts() -> None:
    src_w, crop_w = 1920, 608
    midpoint_left = (src_w - crop_w) / 2.0  # 656.0 (empty background gap!)
    left_face = FaceDetection(300, 250, 140, 160)   # cx=370 -> crop left ~66
    right_face = FaceDetection(1440, 250, 140, 160)  # cx=1510 -> crop left ~1206

    samples: list[_FrameSample] = []
    # 0.0-3.0s: Left speaker talking; brief listening laugh from right at 1.0s must NOT trigger ping-pong!
    for i in range(15):
        t = i * 0.2
        r_conf = 0.22 if t == 1.0 else 0.05
        l_conf = 0.28
        # Temporary face occlusion of left speaker at t=1.6s (dropout tolerance)
        tracks = [(2, right_face, r_conf)] if abs(t - 1.6) < 0.05 else [(1, left_face, l_conf), (2, right_face, r_conf)]
        if abs(t - 1.6) < 0.05:
            # Simulate complete 0-face brief dropout
            tracks = []
        samples.append(_FrameSample(t, is_cut=False, vad=0.8, tracks=tracks, saliency_x=src_w * 0.5))

    # 3.0-6.0s: Right speaker takes over clearly -> must clean-cut to right host (never hover at midpoint!)
    for i in range(15, 30):
        t = i * 0.2
        samples.append(_FrameSample(t, is_cut=False, vad=0.85, tracks=[(1, left_face, 0.04), (2, right_face, 0.45)], saliency_x=src_w * 0.5))

    # 6.0-8.0s: Genuine camera cut to single-person center close-up -> instant cut!
    center_closeup = FaceDetection(820, 200, 260, 300)
    for i in range(30, 40):
        t = i * 0.2
        samples.append(_FrameSample(t, is_cut=(i == 30), vad=0.7, tracks=[(3, center_closeup, 0.4)], saliency_x=src_w * 0.5))

    metrics = FramingMetrics(mode_used="active_speaker")
    kfs = _build_single_track_keyframes(samples, src_w, crop_w, mode="active_speaker", metrics=metrics)
    assert metrics.midpoint_avoided_frames >= 20
    assert metrics.speaker_switches == 1  # Switched once cleanly at 3.0s, zero ping-pong!
    assert metrics.scene_cuts == 1

    # Verify early frames stay locked on left speaker, NOT empty midpoint (656px)
    early_lefts = [k.left for k in kfs if k.time < 2.8]
    assert all(left < 200.0 for left in early_lefts)

    # Verify later two-shot frames cut cleanly (cut=True) to right speaker (>1000px)
    right_kfs = [k for k in kfs if 3.2 <= k.time <= 5.8]
    assert any(k.cut for k in kfs if 2.8 <= k.time <= 3.6)
    assert all(k.left > 1000.0 for k in right_kfs)
    # No frame during two-shot is parked in the empty midpoint zone [450..850]
    two_shot_kfs = [k for k in kfs if k.time < 5.9]
    assert not any(450.0 < k.left < 850.0 for k in two_shot_kfs), f"Midpoint bug: {[k.left for k in two_shot_kfs]}"

    # Expression uses step hold (no linear pan across empty gap) before cut keyframe
    expr = keyframes_to_crop_x_expression(kfs, midpoint_left)
    assert "if(lt(t," in expr


def test_split_screen_auto_returns_to_single_on_closeup_cut_and_places_captions_safely(tmp_path: Path) -> None:
    src_w, src_h, crop_w = 1920, 1080, 608
    left_f = FaceDetection(320, 240, 140, 160)
    right_f = FaceDetection(1420, 240, 140, 160)
    closeup_f = FaceDetection(840, 180, 260, 300)

    samples = []
    # Scene 0 (0-4s): Wide 2-shot with overlapping speech
    for i in range(20):
        samples.append(_FrameSample(i * 0.2, is_cut=False, vad=0.85, tracks=[(1, left_f, 0.35), (2, right_f, 0.32)], saliency_x=960))
    # Scene 1 (4-7s): Camera cut to single close-up
    for i in range(20, 35):
        samples.append(_FrameSample(i * 0.2, is_cut=(i == 20), vad=0.80, tracks=[(3, closeup_f, 0.40)], saliency_x=960))

    metrics = FramingMetrics(mode_used="auto")
    plan = _build_scene_adaptive_plan(samples, 7.0, src_w, src_h, crop_w, (1080, 1920), mode="auto", metrics=metrics)
    assert plan.has_split
    assert len(plan.segments) == 2
    assert plan.segments[0].kind == "split"
    assert plan.segments[1].kind == "single"  # Automatically returned to single-speaker on close-up!
    assert plan.segments[0].top_box is not None and plan.segments[0].bottom_box is not None

    # Split-screen ASS captions use middle-seam alignment (5) so neither face is obstructed
    ass_path = tmp_path / "split.ass"
    write_ass(
        Transcript(7.0, "id", [Segment(0.5, 3.0, "Ini tes subtitle split screen")]),
        Highlight(0.0, 7.0, "Split", "", "", 85),
        ass_path,
        (1080, 1920),
        "Clean",
        split_screen=True,
    )
    ass_text = ass_path.read_text(encoding="utf-8-sig")
    assert ",5," in ass_text  # ASS alignment=5 (center seam)

    # Verify filter_complex generates valid multi-segment split+single graph
    fargs, vmap = _build_filter_args(plan, crop_w, src_h, 0, 656.0, (1080, 1920), captions=True)
    assert fargs[0] == "-filter_complex"
    assert "vstack=inputs=2" in fargs[1]
    assert "concat=n=2:v=1:a=0" in fargs[1]
    assert vmap == "[vout]"


def test_render_fit_original_and_split_screen_end_to_end(tmp_path: Path) -> None:
    mgr = ProcessManager()
    ffmpeg = find_media_tool("ffmpeg")
    src = tmp_path / "wide_16x9.mp4"
    mgr.run([
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=24:duration=4",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", str(src),
    ])
    tr = Transcript(4.0, "id", [Segment(0.2, 3.5, "Halo tes framing vertikal.")])
    hl = Highlight(0.0, 3.5, "FitTest", "", "", 80)

    # Fit Original Video renders clean 9:16 with blurred background
    out_fit = tmp_path / "fit.mp4"
    dur_fit = render_clip(
        src, hl, tr, out_fit, tmp_path / "work_fit", (360, 640), "9:16",
        True, True, "Clean", False, mgr, lambda _m: None, reframe_mode="fit_original",
    )
    assert out_fit.is_file() and dur_fit > 2.5


def test_new_project_smart_reframe_selector_ui(tmp_path: Path, monkeypatch) -> None:
    app = _app()
    from autoclip_studio.gui import MainWindow

    monkeypatch.setattr("autoclip_studio.gui.Database", lambda: Database(tmp_path / "ui.db"))
    win = MainWindow()
    try:
        win.show()
        win.show_page(1)
        assert win.reframe_combo.currentData() == "auto"
        codes = [win.reframe_combo.itemData(i) for i in range(win.reframe_combo.count())]
        assert codes == ["auto", "active_speaker", "split_screen", "single_subject", "fit_original", "off"]
        win.reframe_combo.setCurrentIndex(codes.index("off"))
        assert not win.face_tracking.isChecked()
        win.reframe_combo.setCurrentIndex(codes.index("split_screen"))
        assert win.face_tracking.isChecked()
    finally:
        win.close()
        app.processEvents()
        win.deleteLater()
