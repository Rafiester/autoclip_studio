from __future__ import annotations

import hashlib
import json
import os
from tempfile import NamedTemporaryFile
from pathlib import Path
from typing import Callable

from .models import Segment, Transcript, Word
from .paths import CACHE_DIR, MODEL_CACHE
from .process import CancelledError, ProcessManager


def transcript_cache_path(media_path: Path, model: str, language: str | None = None) -> Path:
    stat = media_path.stat()
    key = hashlib.sha256(
        f"v2:{media_path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}:{model}:{language or 'auto'}".encode()
    ).hexdigest()[:20]
    return CACHE_DIR / "transcripts" / f"{key}.json"


def transcribe(media_path: Path, model_name: str, device_preference: str,
               language: str | None, cache: bool, manager: ProcessManager,
               log: Callable[[str], None]) -> Transcript:
    cache_path = transcript_cache_path(media_path, model_name, language) if cache else None
    manager.check_cancelled()
    if cache_path is not None and cache_path.exists():
        try:
            value = Transcript.from_dict(json.loads(cache_path.read_text(encoding="utf-8")))
            if value.segments:
                log(f"Using cached transcript ({len(value.segments)} segments)")
                return value
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass

    from faster_whisper import WhisperModel

    model_options = {"download_root": os.environ.get("HF_HUB_CACHE", str(MODEL_CACHE))}

    device = _select_device(device_preference)
    compute_type = "float16" if device == "cuda" else "int8"
    log(f"Loading Faster-Whisper {model_name} on {device} ({compute_type})")
    try:
        model = WhisperModel(model_name, device=device, compute_type=compute_type, **model_options)
    except Exception as error:
        if device != "cuda":
            raise
        log(f"CUDA initialization failed ({error}); falling back to CPU")
        device, compute_type = "cpu", "int8"
        model = WhisperModel(model_name, device=device, compute_type=compute_type, **model_options)
    try:
        segments, info = _consume(model, media_path, language, manager, log)
    except CancelledError:
        raise
    except Exception as error:
        if device != "cuda":
            raise
        log(f"CUDA transcription failed ({error}); retrying on CPU")
        del model
        model = WhisperModel(model_name, device="cpu", compute_type="int8", **model_options)
        segments, info = _consume(model, media_path, language, manager, log)
    if not segments:
        raise RuntimeError("No speech was detected in the source video.")
    transcript = Transcript(float(info.duration), str(info.language), segments)
    manager.check_cancelled()
    if cache_path is not None:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        stream = NamedTemporaryFile(mode="w", encoding="utf-8", dir=cache_path.parent, delete=False)
        temp = Path(stream.name)
        try:
            with stream:
                json.dump(transcript.to_dict(), stream, ensure_ascii=False)
            temp.replace(cache_path)
        finally:
            temp.unlink(missing_ok=True)
    return transcript


def _consume(model: object, media_path: Path, language: str | None,
             manager: ProcessManager, log: Callable[[str], None]) -> tuple[list[Segment], object]:
    iterator, info = model.transcribe(
        str(media_path), language=language or None, beam_size=5, vad_filter=True,
        word_timestamps=True, condition_on_previous_text=False,
    )
    segments: list[Segment] = []
    for item in iterator:
        manager.check_cancelled()
        words = [
            Word(float(word.start or item.start), float(word.end or item.end), word.word.strip())
            for word in (item.words or []) if word.word.strip()
        ]
        segments.append(Segment(float(item.start), float(item.end), item.text.strip(), words))
        if len(segments) % 10 == 0:
            log(f"Transcribed {item.end:.0f}s…")
    return segments, info


def _select_device(preference: str) -> str:
    if preference in {"cpu", "cuda"}:
        return preference
    try:
        import ctranslate2
        if "float16" in ctranslate2.get_supported_compute_types("cuda"):
            return "cuda"
    except Exception:
        pass
    return "cpu"

