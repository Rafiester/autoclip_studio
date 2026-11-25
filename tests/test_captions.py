from autoclip_studio.captions import ass_timestamp, write_ass
from autoclip_studio.models import Highlight, Segment, Transcript, Word


def test_ass_caption_generation(tmp_path):
    transcript = Transcript(5, "en", [Segment(0, 2, "hello world", [Word(0, 1, "hello"), Word(1, 2, "world")])])
    target = tmp_path / "captions.ass"
    write_ass(transcript, Highlight(0, 3, "", "", "", 50), target, (1080, 1920), "Impact")
    content = target.read_text(encoding="utf-8-sig")
    assert "PlayResX: 1080" in content
    assert "{\\kf100}HELLO" in content
    assert "Dialogue:" in content


def test_timestamp():
    assert ass_timestamp(61.25) == "0:01:01.25"

