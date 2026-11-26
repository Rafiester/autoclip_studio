from __future__ import annotations

import json
import os
from pathlib import Path

from PySide6.QtWidgets import QApplication

from autoclip_studio.config import SettingsStore
from autoclip_studio.database import Database
from autoclip_studio.heatmap import (
    HeatmapPoint, detect_highlight_regions, parse_heatmap, region_replay_score, summarize_heatmap,
)
from autoclip_studio.highlights import combine_scores, select_highlights
from autoclip_studio.models import Highlight, ProjectOptions, Segment, Transcript
from autoclip_studio.pipeline import Pipeline
from autoclip_studio.process import ProcessManager
from autoclip_studio.youtube_metadata import YouTubeMetadata
from autoclip_studio.youtube_subtitles import (
    parse_vtt_text, select_subtitle_track, validate_youtube_transcript,
)


def _app() -> QApplication:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QApplication.instance() or QApplication([])


def test_heatmap_parsing_never_fabricates_and_merges_overlapping_peaks() -> None:
    assert parse_heatmap(None) == []
    assert parse_heatmap([]) == []
    assert parse_heatmap([{"start_time": 0, "end_time": 5, "value": 0.0}]) == []

    # Two close overlapping peaks near 30s and 40s, plus a distinct peak near 150s
    raw = []
    for i in range(40):
        start = i * 5.0
        end = start + 5.0
        val = 0.2
        if 25 <= start <= 45:
            val = 0.95 if start in (30, 40) else 0.78
        elif 145 <= start <= 160:
            val = 0.92 if start == 150 else 0.75
        raw.append({"start_time": start, "end_time": end, "value": val})

    points = parse_heatmap(raw, duration=200.0)
    assert len(points) == 40
    assert all(0.0 <= p.value <= 1.0 for p in points)

    regions = detect_highlight_regions(points, min_duration=20.0, max_duration=50.0, video_duration=200.0)
    assert len(regions) == 2
    # Regions must not overlap
    ordered = sorted(regions, key=lambda r: r.start)
    assert ordered[0].end <= ordered[1].start
    assert all(r.replay_score >= 60 for r in regions)


def test_deterministic_hybrid_and_most_replayed_scoring() -> None:
    assert combine_scores(80, 100, mode="hybrid", ai_weight=0.6) == 88
    assert combine_scores(80, 100, mode="hybrid", ai_weight=0.5) == 90
    assert combine_scores(80, 100, mode="ai", ai_weight=0.6) == 80
    assert combine_scores(60, 95, mode="most_replayed") == 88
    assert combine_scores(75, None, mode="hybrid", ai_weight=0.6) == 75


def test_hybrid_analysis_combines_replay_peaks_and_outside_ai_moments() -> None:
    segments = [
        Segment(0.0, 15.0, "Intro pembukaan video ini sangat penting."),
        Segment(15.0, 32.0, "Ini momen paling sering diputar ulang oleh penonton."),
        Segment(32.0, 50.0, "Penjelasan lengkap kenapa strategi ini berhasil."),
        Segment(50.0, 90.0, "Bagian transisi biasa saja."),
        Segment(90.0, 118.0, "Rahasia besar yang jarang diketahui orang tentang editing cepat!"),
        Segment(118.0, 130.0, "Kesimpulan akhir yang jelas."),
    ]
    transcript = Transcript(130.0, "id", segments)
    points = [
        HeatmapPoint(i * 10.0, (i + 1) * 10.0, 0.95 if 10 <= i * 10 < 50 else 0.15)
        for i in range(13)
    ]

    captured_prompts: list[tuple[str, str]] = []

    class FakeProvider:
        kind = "gemini"
        model = "gemini-2.5-flash"

        def complete(self, system_prompt: str, user_prompt: str) -> str:
            captured_prompts.append((system_prompt, user_prompt))
            return json.dumps({
                "highlights": [
                    {
                        "start": 15.0, "end": 50.0,
                        "title": "Momen Replay", "hook": "Ini momen paling sering diputar",
                        "description": "Peak replay", "score": 82, "reason": "High replay + strong context",
                    },
                    {
                        "start": 90.0, "end": 118.0,
                        "title": "Rahasia Editing", "hook": "Rahasia besar yang jarang diketahui",
                        "description": "Outside peak", "score": 96, "reason": "Strong standalone AI moment outside replay peak",
                    },
                ]
            })

    logs: list[str] = []
    results = select_highlights(
        transcript, 2, 20, 60, FakeProvider(), logs.append,
        mode="hybrid", heatmap_points=points, ai_weight=0.6,
    )
    assert len(results) == 2
    assert any("YOUTUBE AUDIENCE REPLAY PEAKS" in user_p for _, user_p in captured_prompts)
    by_title = {h.title: h for h in results}
    assert "Momen Replay" in by_title and "Rahasia Editing" in by_title
    # Deterministic backend score check: 60% AI + 40% Replay
    peak_h = by_title["Momen Replay"]
    assert peak_h.ai_score == 82
    assert peak_h.replay_score == region_replay_score(15.0, 50.0, points)
    assert peak_h.score == combine_scores(82, peak_h.replay_score, mode="hybrid", ai_weight=0.6)
    assert peak_h.detection_mode == "hybrid"

    # Most Replayed mode prioritizes peak without calling AI
    mr_results = select_highlights(
        transcript, 1, 20, 60, None, logs.append,
        mode="most_replayed", heatmap_points=points,
    )
    assert len(mr_results) == 1
    assert mr_results[0].detection_mode == "most_replayed"
    assert (mr_results[0].replay_score or 0) >= 70


def test_youtube_vtt_normalization_deduplicates_rolling_auto_captions_and_prefers_manual() -> None:
    rolling_vtt = """WEBVTT
Kind: captions
Language: id

00:00:00.000 --> 00:00:02.000
Halo <00:00:00.500><c>semuanya</c> selamat datang

00:00:02.000 --> 00:00:04.000
Halo semuanya selamat datang
di channel ini hari ini

00:00:04.000 --> 00:00:06.500
di channel ini hari ini
kita akan bahas tips penting
"""
    segments = parse_vtt_text(rolling_vtt, is_auto=True)
    assert len(segments) == 3
    assert all(seg.words == [] for seg in segments)  # Never fabricates word timings
    joined = " ".join(seg.text for seg in segments)
    assert joined.count("Halo semuanya") == 1
    assert joined.count("di channel ini hari ini") == 1
    assert "tips penting" in joined

    ok, _ = validate_youtube_transcript(segments, 7.0)
    assert ok

    # Sparse coverage fails validation -> falls back to Whisper
    sparse_ok, reason = validate_youtube_transcript(segments, 180.0)
    assert not sparse_ok
    assert "early" in reason.lower() or "sparse" in reason.lower()

    meta = YouTubeMetadata(
        "dQw4w9WgXcQ", "Demo ID", "Channel", 120, "1080 × 1920", 1000, "2026-01-01",
        subtitle_languages=("id", "en"), auto_caption_languages=("id", "en"), language="id",
    )
    assert select_subtitle_track(meta, preferred_language="id") == ("id", False)
    meta_auto_only = YouTubeMetadata(
        "dQw4w9WgXcQ", "Demo Auto", "Channel", 120, "1080 × 1920", 1000, "2026-01-01",
        subtitle_languages=(), auto_caption_languages=("id", "en"), language="id",
    )
    assert select_subtitle_track(meta_auto_only, preferred_language="id") == ("id", True)


def test_ui_heatmap_mode_fallback_and_clip_score_breakdown(tmp_path: Path, monkeypatch) -> None:
    app = _app()
    from autoclip_studio.gui import MainWindow

    monkeypatch.setattr("autoclip_studio.gui.Database", lambda: Database(tmp_path / "ui.db"))
    win = MainWindow()
    try:
        win.show()
        win.show_page(win.PAGE_PROJECT)

        # YouTube metadata WITH heatmap -> defaults to Hybrid
        meta_with_hm = YouTubeMetadata(
            "dQw4w9WgXcQ", "Video With Heatmap", "Creator", 180, "1920 × 1080", 50000, "2026-01-01",
            heatmap=tuple(
                {"start_time": i * 10.0, "end_time": (i + 1) * 10.0, "value": 0.95 if i in (3, 4, 12) else 0.2}
                for i in range(18)
            ),
            subtitle_languages=("id",), auto_caption_languages=("id", "en"), language="id",
        )
        win._update_project_name(meta_with_hm)
        app.processEvents()
        assert win.highlight_mode_combo.currentData() == "hybrid"
        assert win.heatmap_widget.isVisible()
        assert "Available" in win.heatmap_widget.availability_chip.text()
        assert "Manual: id" in win.yt_subtitles_hint.text()

        # YouTube metadata WITHOUT heatmap -> falls back to AI Analysis with clear notice
        meta_no_hm = YouTubeMetadata(
            "abcdefghijk", "Video No Heatmap", "Creator", 180, "1920 × 1080", 100, "2026-01-01",
        )
        win._update_project_name(meta_no_hm)
        app.processEvents()
        assert win.highlight_mode_combo.currentData() == "ai"
        assert win.highlight_mode_note.isVisible()
        assert "unavailable" in win.highlight_mode_note.text().lower()

        # Local File tab -> falls back to AI Analysis with clear notice
        win.set_source_tab(1)
        app.processEvents()
        assert win.highlight_mode_combo.currentData() == "ai"
        assert "local video" in win.highlight_mode_note.text().lower()

        # Generated Clips detail panel shows Combined, AI, Replay scores & selection reason
        win.show_page(win.PAGE_CLIPS)
        clip_file = tmp_path / "sample.mp4"
        clip_file.write_bytes(b"fake")
        win.clip_detail.display_clip({
            "id": "c1", "path": str(clip_file), "title": "Peak Hook",
            "duration": 32.0, "score": 88, "ai_score": 82, "replay_score": 97,
            "detection_mode": "hybrid", "reason": "Strong opening hook aligned with top replay peak.",
            "description": "Clip description",
        })
        assert "Combined 88/100" in win.clip_detail.score_badge.text()
        assert "AI: 82/100" in win.clip_detail.ai_score_chip.text()
        assert "Replay: 97/100" in win.clip_detail.replay_score_chip.text()
        assert "Hybrid" in win.clip_detail.mode_chip.text()
        assert "Strong opening hook" in win.clip_detail.reason_label.text()
    finally:
        win.close()
        app.processEvents()
        win.deleteLater()
