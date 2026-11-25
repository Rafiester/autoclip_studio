from autoclip_studio.captions import TEMPLATES, ass_timestamp, template_font, write_ass
from autoclip_studio.models import Highlight, Segment, Transcript, Word


def test_ass_caption_generation(tmp_path):
    transcript = Transcript(5, "en", [Segment(0, 2, "hello world", [Word(0, 1, "hello"), Word(1, 2, "world")])])
    target = tmp_path / "captions.ass"
    write_ass(transcript, Highlight(0, 3, "", "", "", 50), target, (1080, 1920), "Impact")
    content = target.read_text(encoding="utf-8-sig")
    assert "PlayResX: 1080" in content
    assert r"\t(0,105,\fscx111\fscy111)" in content
    assert "}HELLO WORLD" in content


def test_karaoke_uses_actual_word_offsets_and_clip_bounds(tmp_path):
    words = [Word(1, 1.52, "One"), Word(1.7, 2.25, "two"), Word(2.4, 2.95, "three")]
    transcript = Transcript(3, "en", [Segment(1, 2.95, "One two three", words)])
    target = tmp_path / "karaoke.ass"
    write_ass(transcript, Highlight(1.25, 2.8, "", "", "", 0), target, (1080, 1920), "Karaoke")
    content = target.read_text(encoding="utf-8-sig")
    assert r"{\kt0\kf27}One {\kt45\kf55}two {\kt115\kf40}three" in content
    assert "0:00:00.00,0:00:01.55" in content


def test_word_effects_use_segment_fallback_for_missing_or_incomplete_timing(tmp_path):
    target = tmp_path / "captions.ass"
    for words in ([], [Word(1, 1.5, "Hello"), Word(1.5, 1.5, "there")]):
        transcript = Transcript(4, "en", [Segment(1, 2.5, "Hello there", words)])
        for name in ("Karaoke", "Hormozi", "Pop Bounce", "Box Highlight", "Typewriter", "Viral Shorts"):
            write_ass(transcript, Highlight(0, 4, "", "", "", 0), target, (480, 854), name)
            content = target.read_text(encoding="utf-8-sig")
            assert content.count("Dialogue:") == 1
            assert "0:00:01.00,0:00:02.50" in content
            assert "hello there" in content.casefold()
            assert r"\kt" not in content


def test_templates_emit_distinct_ass_effects(tmp_path):
    words = [Word(0, 0.36, "Hello"), Word(0.4, 1.2, "world")]
    transcript = Transcript(2, "en", [Segment(0, 1.2, "Hello world", words)])
    target = tmp_path / "animated.ass"
    effects = {
        "Impact": r"\fscx111", "Clean": r"\fad(140,140)", "Neon": r"\blur",
        "Karaoke": r"\kf36", "Hormozi": r"\fscy112", "Pop Bounce": r"\fscx122",
        "Box Highlight": r"\3a&H65&\t(0,95,\3a&H00&)", "Cinematic": r"\fad(300,300)",
        "Gradient": r"\t(0,280,\fsp0)", "Typewriter": ",,Hell\n", "Podcast": r"\fad(90,0)",
        "Viral Shorts": r"\move",
    }
    assert effects.keys() == TEMPLATES.keys()
    for name, effect in effects.items():
        write_ass(transcript, Highlight(0, 2, "", "", "", 0), target, (1080, 1920), name)
        content = target.read_text(encoding="utf-8-sig")
        assert effect in content, name
        assert f"Style: Default,{template_font(TEMPLATES[name])}," in content


def test_timestamp():
    assert ass_timestamp(61.25) == "0:01:01.25"
    assert ass_timestamp(59.999) == "0:01:00.00"

