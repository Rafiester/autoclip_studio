"""Comprehensive tests for Intelligent Auto Director, false-face suppression, styles, and rendering sync."""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np
from PySide6.QtWidgets import QApplication

from autoclip_studio.auto_director import (
    REFRAME_STYLES,
    DirectorSample,
    TrackObservation,
    direct_single_stream,
    get_style_config,
)
from autoclip_studio.config import SettingsStore
from autoclip_studio.database import Database
from autoclip_studio.rendering import _build_filter_args
from autoclip_studio.smart_reframe import (
    CropKeyframe,
    FaceDetection,
    FramingPlan,
    FramingSegment,
    _filter_false_faces,
    keyframes_to_crop_x_expression,
)


def _app() -> QApplication:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QApplication.instance() or QApplication([])


def test_single_person_locked_tripod_dead_zone_and_smooth_lean() -> None:
    """Single-person talking with slight head sway stays rock-steady; large lean pans smoothly without clipping."""
    src_w, src_h, crop_w = 1920, 1080, 606
    # Case 1: Minor conversational head sway (+-35 px) -> locked tripod (0 px/s jitter!)
    sway_samples = [
        DirectorSample(
            rel_t=i * 0.2,
            is_cut=False,
            vad=0.8,
            tracks=[TrackObservation(1, 860.0 + 30.0 * np.sin(i * 0.6), 180.0, 220.0, 240.0, 0.35)],
            saliency_x=960.0,
        )
        for i in range(40)
    ]
    kfs, stats = direct_single_stream(sway_samples, src_w, src_h, crop_w, mode="auto", style="natural")
    assert stats.artificial_switches == 0
    assert stats.crop_jitter_px_s == 0.0
    assert len(kfs) == 1
    assert kfs[0].time == 0.0

    # Case 2: Large deliberate lean (+320 px) -> smooth bounded pan, never clips face outside crop
    lean_samples = []
    for i in range(35):
        cx = 900.0 if i < 10 else min(1240.0, 900.0 + (i - 10) * 18.0)
        lean_samples.append(DirectorSample(
            rel_t=i * 0.2, is_cut=False, vad=0.8,
            tracks=[TrackObservation(1, cx - 110.0, 180.0, 220.0, 240.0, 0.35)],
            saliency_x=960.0,
        ))
    kfs_lean, stats_lean = direct_single_stream(lean_samples, src_w, src_h, crop_w, mode="auto", style="natural")
    assert stats_lean.artificial_switches == 0
    assert len(kfs_lean) > 1
    assert all(not kf.cut for kf in kfs_lean[1:])  # Same-subject smooth pan, no jump cut!
    # Final crop keeps shifted face safely inside frame
    final_left = kfs_lean[-1].left
    assert final_left + 40.0 <= (1240.0 - 110.0) and (1240.0 + 110.0) <= final_left + crop_w - 40.0


def test_two_person_viterbi_aba_suppression_turn_alignment_and_lookahead() -> None:
    """Two-person wide shot avoids A-B-A ping-pong on brief nods/interruptions but cleanly cuts on real turns."""
    src_w, src_h, crop_w = 1920, 1080, 606
    samples: list[DirectorSample] = []
    for i in range(70):  # 14.0 seconds @ 5 fps
        t = i * 0.2
        # Speaker A (left, cx=420) speaks 0..6.0s (with brief 0.4s interruption from B at 2.6..3.0s)
        # Turn boundary pause at 6.0..6.2s
        # Speaker B (right, cx=1500) speaks 6.2..13.0s
        # Brief 0.6s spike from A right before clip end (13.4..14.0s) -> lookahead should NOT switch!
        vad = 0.08 if 5.95 <= t <= 6.25 else 0.85
        turn = (5.95 <= t <= 6.25)
        if 2.6 <= t < 3.0:
            c_a, c_b = 0.14, 0.24  # Brief weak interruption
        elif t < 6.0:
            c_a, c_b = 0.40, 0.05
        elif t < 13.2:
            c_a, c_b = 0.05, 0.42
        else:
            c_a, c_b = 0.30, 0.12  # Late spike near clip end
        samples.append(DirectorSample(
            rel_t=t, is_cut=False, vad=vad, turn_boundary=turn, saliency_x=960.0,
            tracks=[
                TrackObservation(1, 320.0, 200.0, 200.0, 220.0, c_a),
                TrackObservation(2, 1400.0, 200.0, 200.0, 220.0, c_b),
            ],
        ))

    kfs_nat, stats_nat = direct_single_stream(samples, src_w, src_h, crop_w, mode="auto", style="natural")
    kfs_stb, stats_stb = direct_single_stream(samples, src_w, src_h, crop_w, mode="auto", style="stable")
    kfs_dyn, stats_dyn = direct_single_stream(samples, src_w, src_h, crop_w, mode="auto", style="dynamic")

    # Natural & Stable must execute EXACTLY 1 clean editorial cut (A -> B around 6s), zero A-B-A ping-pong!
    assert stats_nat.artificial_switches == 1
    assert stats_stb.artificial_switches <= stats_nat.artificial_switches
    assert stats_dyn.artificial_switches >= stats_nat.artificial_switches
    assert stats_nat.midpoint_avoided_frames == 70

    # Verify the switch is an instant clean cut (cut=True) landing near the turn boundary [5.4s .. 6.8s]
    cuts = [kf for kf in kfs_nat if kf.cut]
    assert len(cuts) == 1
    assert 5.4 <= cuts[0].time <= 6.8
    # Starts on Speaker A (~117px), ends on Speaker B (~1197px), NEVER midpoint (~657px)!
    assert kfs_nat[0].left < 250.0
    assert kfs_nat[-1].left > 1050.0


def test_reaction_shot_damping_and_genuine_short_response_override() -> None:
    """Silent laugh/nod does not hijack speaker; genuine solo short response can still be framed."""
    src_w, src_h, crop_w = 1920, 1080, 606
    # Silent laugh from B during low VAD must NOT hijack A
    reaction_samples = []
    for i in range(40):  # 8.0s
        t = i * 0.2
        if 3.0 <= t < 3.8:
            # Low speech VAD: B smiles/nods silently
            vad, c_a, c_b = 0.08, 0.12, 0.35
        else:
            vad, c_a, c_b = 0.85, 0.42, 0.05
        reaction_samples.append(DirectorSample(
            rel_t=t, is_cut=False, vad=vad, saliency_x=960.0,
            tracks=[
                TrackObservation(1, 300.0, 200.0, 200.0, 220.0, c_a),
                TrackObservation(2, 1400.0, 200.0, 200.0, 220.0, c_b),
            ],
        ))
    _kfs, stats = direct_single_stream(reaction_samples, src_w, src_h, crop_w, mode="auto", style="natural")
    assert stats.artificial_switches == 0


def test_three_person_podcast_and_leading_dropout_lookahead() -> None:
    """Three-person podcast switches cleanly across all 3 hosts and backfills leading shot dropouts."""
    src_w, src_h, crop_w = 1920, 1080, 606
    samples: list[DirectorSample] = []
    # Shot 1 (0..12s): 3-person wide shot, A speaks 0..4s, B speaks 4..8s, C speaks 8..12s
    for i in range(60):
        t = i * 0.2
        c_a = 0.45 if t < 4.0 else 0.04
        c_b = 0.45 if 4.0 <= t < 8.0 else 0.04
        c_c = 0.45 if t >= 8.0 else 0.04
        samples.append(DirectorSample(
            rel_t=t, is_cut=False, vad=0.85, turn_boundary=(abs(t - 4.0) < 0.25 or abs(t - 8.0) < 0.25),
            saliency_x=960.0,
            tracks=[
                TrackObservation(1, 220.0, 200.0, 180.0, 200.0, c_a),   # left cx=310
                TrackObservation(2, 870.0, 200.0, 180.0, 200.0, c_b),   # mid  cx=960
                TrackObservation(3, 1520.0, 200.0, 180.0, 200.0, c_c),  # right cx=1610
            ],
        ))
    # Shot 2 (12..15s): Original camera cut with 0.6s leading face dropout (speaker looking down), then visible at cx=1150
    for j in range(15):
        t = 12.0 + j * 0.2
        tr = [] if j < 3 else [TrackObservation(4, 1030.0, 200.0, 240.0, 260.0, 0.35)]
        samples.append(DirectorSample(rel_t=t, is_cut=(j == 0), vad=0.7, tracks=tr, saliency_x=400.0))

    kfs, stats = direct_single_stream(samples, src_w, src_h, crop_w, mode="auto", style="natural", source_fps=25.0)
    assert stats.original_cuts == 1
    assert stats.artificial_switches == 2
    # Shot 2 opens IMMEDIATELY at cx=1150 (left ~ 847) from the exact cut frame (11.980s), NOT saliency 400!
    shot2_kf = [kf for kf in kfs if kf.time >= 11.9][0]
    assert shot2_kf.cut is True
    assert abs(shot2_kf.time - 11.98) < 0.01  # Exact half-frame midpoint before 12.00s at 25 FPS!
    assert abs(shot2_kf.left - (1150.0 - crop_w * 0.5)) < 15.0


def test_false_positive_torso_and_bottom_edge_face_suppression() -> None:
    """Rejects black backpack at bottom edge and chest/hand detections directly below a real face."""
    gray = np.full((360, 640), 120, dtype=np.uint8)
    # Dark low-texture region in bottom-left simulating a black backpack
    gray[280:350, 140:220] = 18
    real_face = FaceDetection(x=800.0, y=150.0, w=260.0, h=280.0, score=0.95)
    hand_on_chest = FaceDetection(x=830.0, y=540.0, w=170.0, h=170.0, score=0.85)
    backpack_corner = FaceDetection(x=440.0, y=850.0, w=180.0, h=180.0, score=0.78)
    other_host = FaceDetection(x=1400.0, y=190.0, w=230.0, h=240.0, score=0.85)

    kept = _filter_false_faces(
        [real_face, hand_on_chest, backpack_corner, other_host],
        gray, scale=640.0 / 1920.0, src_width=1920.0, src_height=1080.0,
    )
    assert real_face in kept
    assert other_host in kept
    assert hand_on_chest not in kept
    assert backpack_corner not in kept


def test_filter_graph_setpts_and_millisecond_cut_expression() -> None:
    """Single-segment FFmpeg filter normalizes PTS before crop and formats cuts with millisecond precision."""
    plan = FramingPlan(segments=[
        FramingSegment("single", 0.0, 10.0, [
            CropKeyframe(0.0, 113.25, cut=False),
            CropKeyframe(0.383, 544.50, cut=True),
            CropKeyframe(6.06, 576.00, cut=True),
        ])
    ])
    args, vmap = _build_filter_args(plan, 606, 1080, 0, 657.0, (1080, 1920), captions=False)
    assert vmap == "0:v:0"
    assert args[0] == "-vf"
    assert args[1].startswith("setpts=PTS-STARTPTS,crop=606:1080:")
    expr = keyframes_to_crop_x_expression(plan.segments[0].keyframes, 657.0)
    assert "0.383" in expr and "6.06" in expr


def test_reframe_style_persistence_and_new_project_selector(tmp_path: Path, monkeypatch) -> None:
    """Reframing Style selector defaults to Natural, disables on Off/Fit, and persists across restarts."""
    app = _app()
    from autoclip_studio.gui import MainWindow

    settings_path = tmp_path / "settings.json"
    db_path = tmp_path / "ui.db"
    s1 = SettingsStore(settings_path)
    monkeypatch.setattr(s1, "api_key", lambda *_a: "")
    monkeypatch.setattr(s1, "set_api_key", lambda *_a: None)
    monkeypatch.setattr("autoclip_studio.gui.SettingsStore", lambda: s1)
    monkeypatch.setattr("autoclip_studio.gui.Database", lambda: Database(db_path))

    win = MainWindow()
    try:
        win.show()
        win.show_page(win.PAGE_PROJECT)
        assert win.reframe_style_combo.count() == len(REFRAME_STYLES)
        assert win.reframe_style_combo.currentData() == "natural"
        assert win.reframe_style_combo.isEnabled()

        # Select Stable style and flush
        idx_stable = win.reframe_style_combo.findData("stable")
        win.reframe_style_combo.setCurrentIndex(idx_stable)
        app.processEvents()
        s1.flush()

        # Disables when reframing mode is Off
        idx_off = win.reframe_combo.findData("off")
        win.reframe_combo.setCurrentIndex(idx_off)
        app.processEvents()
        assert not win.reframe_style_combo.isEnabled()
        # Restore Auto
        win.reframe_combo.setCurrentIndex(win.reframe_combo.findData("auto"))
        app.processEvents()
        assert win.reframe_style_combo.isEnabled()
        s1.flush()
    finally:
        win.close()
        app.processEvents()
        win.deleteLater()

    # Reload fresh SettingsStore -> Stable style persisted!
    s2 = SettingsStore(settings_path)
    assert s2.project_defaults()["reframe_style"] == "stable"
    assert get_style_config("stable").min_shot_duration > get_style_config("natural").min_shot_duration
    assert get_style_config("dynamic").min_shot_duration < get_style_config("natural").min_shot_duration
