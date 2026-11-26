import json
import os
import pytest
from PySide6.QtWidgets import QApplication

from autoclip_studio.captions import write_ass
from autoclip_studio.config import SettingsStore
from autoclip_studio.database import Database
from autoclip_studio.gui import MainWindow
from autoclip_studio.models import Highlight, Segment, Transcript
from autoclip_studio.process import ProcessManager
from autoclip_studio.speech_engine import GpuInfo, SpeechSettings, choose_compute, find_executable, find_model, stage_model
from autoclip_studio.transcription import (
    SpeechProgress, _batch_size, _validated_transcript, export_transcript, transcribe, transcript_cache_path,
)


@pytest.mark.skipif(os.name != "nt", reason="Subtitle Edit APPDATA lookup is Windows-only")
def test_reuses_subtitle_edit_model_without_copying_weights(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    root = tmp_path / "Subtitle Edit/SpeechToText/Purfview-Faster-Whisper-XXL"
    model = root / "_models/faster-whisper-large-v2"
    model.mkdir(parents=True)
    for name in ("model.bin", "tokenizer.json", "config.json"):
        (model / name).write_bytes(b"original")
    exe = root / "faster-whisper-xxl.exe"
    exe.touch()

    assert find_executable() == exe
    assert find_model("large-v2", SpeechSettings(), exe) == model
    staged = stage_model(model, tmp_path / "temporary", "large-v2") / model.name
    assert os.path.samefile(model / "model.bin", staged / "model.bin")
    (staged / "tokenizer.json").write_bytes(b"changed")
    assert (model / "tokenizer.json").read_bytes() == b"original"


def test_selected_compute_and_batch_match_vram_budget():
    gpu = GpuInfo("RTX 3050", 6144, 4200, 1944, 83)
    assert choose_compute("cuda", "auto", gpu) == "int8_float16"
    assert choose_compute("cuda", "float16", gpu) == "float16"
    assert choose_compute("cpu", "float16", gpu) == "int8"
    assert _batch_size(8, "cuda", gpu, "large-v3-turbo") == 1
    assert _batch_size(8, "cuda", gpu, "small") == 2
    assert _batch_size(0, "cuda", gpu, "small") == 1


def test_timestamp_validation_keeps_legitimate_repetition_and_flags_suspicious_silence(tmp_path):
    repeated = [{"start": i, "end": i + 1, "text": "Again.", "avg_logprob": -0.3} for i in range(3)]
    repeated.append({"start": 2.8, "end": 4, "text": "Loop loop loop loop loop loop", "avg_logprob": -1.2,
                     "no_speech_prob": 0.96, "words": [{"start": 0, "end": 9, "word": "Loop"}]})
    logs = []
    transcript = _validated_transcript(repeated, "en", 4, 2, 8, logs.append)
    assert [item.text for item in transcript.segments[:3]] == ["Again."] * 3
    last = transcript.segments[-1]
    assert (last.start, last.end, last.words[0].start, last.words[0].end) == (5, 6, 5, 6)
    assert any("1 low-confidence" in line for line in logs)
    export_transcript(transcript, tmp_path)
    assert json.loads((tmp_path / "transcript.json").read_text(encoding="utf-8")) == transcript.to_dict()
    assert "00:00:05,000 --> 00:00:06,000" in (tmp_path / "transcript.srt").read_text(encoding="utf-8-sig")


def test_captions_without_word_timestamps_use_segment_timing(tmp_path):
    output = tmp_path / "captions.ass"
    write_ass(Transcript(5, "en", [Segment(1, 4, "Some words")]),
              Highlight(0, 5, "", "", "", 0), output, (360, 640), "Clean")
    text = output.read_text(encoding="utf-8-sig")
    assert "0:00:01.00,0:00:04.00" in text
    assert "Some words" in text and "\\kf" not in text


def test_cache_records_engine_settings_and_sampled_source(tmp_path):
    media = tmp_path / "media.mp4"
    media.write_bytes(b"original")
    before = media.stat()
    first = transcript_cache_path(media, "small", "en", identity="python:vad=true")
    assert first != transcript_cache_path(media, "small", "en", identity="purfview:vad=false")
    media.write_bytes(b"modified")
    os.utime(media, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert first != transcript_cache_path(media, "small", "en", identity="python:vad=true")


@pytest.mark.parametrize("cpu_fallback", [True, False])
def test_cuda_oom_tries_lower_memory_and_respects_cpu_policy(monkeypatch, tmp_path, cpu_fallback):
    media = tmp_path / "source.mp4"
    media.touch()
    monkeypatch.setattr("autoclip_studio.transcription.find_model", lambda *_args: None)
    monkeypatch.setattr("autoclip_studio.transcription._python_cuda", lambda: True)
    monkeypatch.setattr("autoclip_studio.transcription._audio_timing", lambda *_args: (0, 5, 5))
    monkeypatch.setattr("autoclip_studio.transcription.gpu_info", lambda: GpuInfo("NVIDIA", 6144, 6000, 144, 5))
    calls = []

    def run(_model, _source, device, compute, batch, *_args):
        calls.append((device, compute, batch))
        if device == "cuda":
            raise RuntimeError("CUDA out of memory")
        return [{"start": 1, "end": 4, "text": "CPU fallback"}], "en", 5.0

    monkeypatch.setattr("autoclip_studio.transcription._run_python", run)
    settings = SpeechSettings(engine="python", cpu_fallback=cpu_fallback)
    args = (media, "small", "cuda", None, False, ProcessManager(), lambda _: None)
    if cpu_fallback:
        assert transcribe(*args, settings=settings).segments[0].text == "CPU fallback"
        assert calls[-1] == ("cpu", "int8", 1)
    else:
        with pytest.raises(RuntimeError, match="out of memory"):
            transcribe(*args, settings=settings)
        assert len(calls) == 2
    assert calls[:2] == [("cuda", "float16", 1), ("cuda", "int8_float16", 1)]


def test_speech_settings_and_progress_ui(monkeypatch, tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    settings = SettingsStore(tmp_path / "settings.json")
    monkeypatch.setattr("autoclip_studio.config.SettingsStore.api_key", lambda *_args: "")
    monkeypatch.setattr("autoclip_studio.gui.SettingsStore", lambda: settings)
    monkeypatch.setattr("autoclip_studio.gui.Database", lambda: Database(tmp_path / "projects.db"))
    window = MainWindow()
    window.show()
    window.show_page(6)
    window.speech_settings.model.setCurrentText("large-v3-turbo")
    window.speech_settings.language.setText("id")
    window.speech_settings.save()
    assert settings.speech().language == "id"
    assert window.whisper_model.currentText() == "large-v3-turbo"
    window.show_page(2)
    window.on_progress(12, "Transcribing")
    window.on_speech_progress(SpeechProgress("Loading model", "Python", "small", "cpu", "int8", 1, duration=80))
    assert window.speech_progress.percent.text() == "—"
    assert window.speech_progress.values["Time left"].text() == "—"
    window.on_speech_progress(SpeechProgress(
        "Transcribing", "Purfview", "large-v3-turbo", "cuda", "float16", 5,
        processed=40, duration=80, eta=5, speed=8, reliable_percentage=True,
    ))
    assert window.speech_progress.percent.text() == "50.0%"
    assert window.speech_progress.values["Speed / RTF"].text() == "8.0× / 0.12"
    window.worker = object()
    window.speech_progress.cancel_button.click()
    assert window.pipeline.manager.cancel_event.is_set()
    assert "Cancelling" in window.speech_progress.stage.text()
    window.worker = None
    window.close()
    app.processEvents()
