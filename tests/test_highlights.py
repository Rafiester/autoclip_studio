from autoclip_studio.highlights import dedupe, local_highlights, parse_highlights
from autoclip_studio.models import Highlight, Segment, Transcript, Word


def test_parse_and_clamp_highlights():
    raw = '```json\n{"highlights":[{"start":-2,"end":35,"title":"A","hook":"B","description":"C","score":94}]}\n```'
    result = parse_highlights(raw, 30)
    assert len(result) == 1
    assert result[0].start == 0
    assert result[0].end == 30
    assert result[0].score == 94


def test_dedupe_prefers_high_score():
    values = [
        Highlight(0, 30, "low", "", "", 50),
        Highlight(2, 29, "high", "", "", 90),
        Highlight(40, 70, "other", "", "", 70),
    ]
    assert [item.title for item in dedupe(values)] == ["high", "other"]


def test_local_selector_returns_complete_clip():
    segments = []
    for index in range(12):
        start = index * 3.0
        text = "Here is an important secret and how it works." if index == 0 else "This explanation has useful detail and a clear result."
        segments.append(Segment(start, start + 3, text, [Word(start, start + 1, "word")]))
    result = local_highlights(Transcript(36, "en", segments), 2, 20, 35)
    assert result
    assert 15 <= result[0].end - result[0].start <= 35

