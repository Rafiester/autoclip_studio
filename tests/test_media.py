from autoclip_studio.media import is_youtube_url, youtube_id


def test_youtube_validation():
    assert is_youtube_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert youtube_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert not is_youtube_url("https://example.com/video")

