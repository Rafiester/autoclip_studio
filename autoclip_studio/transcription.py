from __future__ import annotations

import gc
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import threading
from dataclasses import asdict, dataclass
from importlib.metadata import version
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from time import perf_counter
from typing import Callable

from .media import probe
from .models import Segment, Transcript, Word
from .paths import CACHE_DIR
from .performance import ProfileLimits
from .process import CancelledError, ProcessManager
from .speech_engine import (
    GpuInfo, Purfview, SpeechSettings, choose_compute, download_speech_model, find_executable,
    find_model, gpu_info, inspect_purfview, mlx_whisper_available, model_cache_root,
    python_cuda as _python_cuda, stage_model,
)

MLX_REPOS = {
    "tiny": "mlx-community/whisper-tiny-mlx",
    "base": "mlx-community/whisper-base-mlx",
    "small": "mlx-community/whisper-small-mlx",
    "small-q4": "mlx-community/whisper-small-mlx-q4",
    "medium": "mlx-community/whisper-medium-mlx",
    "large-v2": "mlx-community/whisper-large-v2-mlx",
    "large-v3": "mlx-community/whisper-large-v3-mlx",
    "large-v3-turbo": "mlx-community/whisper-large-v3-turbo",
    "large-v3-turbo-q4": "mlx-community/whisper-large-v3-turbo-q4",
}

_MLX_INFERENCE_LOCK = threading.Lock()


def release_mlx_memory() -> None:
    """Release Apple Silicon MLX Metal cache and Python references safely."""
    try:
        import mlx.core as mx
        clear_cache = getattr(mx, "clear_cache", None) or getattr(mx.metal, "clear_cache", None)
        if clear_cache is not None:
            clear_cache()
    except Exception:
        pass
    gc.collect()


_XXL_PROGRESS = re.compile(r"^\s*\d+%\s*\|\s*(\d+)\s*/\s*(\d+)\s*\|\s*([\d:]+)<<([\d:]+)\s*\|\s*([\d.]+)\s+audio seconds/s")
_CUDA_ERROR = re.compile(
    r"cublas|cudnn|out of memory|failed to allocate|cuda(?: runtime)? error|cuda.*not (?:found|supported|available)"
    r"|failed to (?:initialize|load).*cuda", re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class SpeechProgress:
    stage: str
    engine: str
    model: str
    device: str
    compute: str
    elapsed: float
    processed: float | None = None
    duration: float | None = None
    eta: float | None = None
    speed: float | None = None
    gpu: GpuInfo | None = None
    reliable_percentage: bool = False


class _Reporter:
    def __init__(self, model: str, callback: Callable[[SpeechProgress], None] | None) -> None:
        self.model = model
        self.callback = callback
        self.start = perf_counter()
        self.device = self.compute = self.engine = ""
        self.gpu = None
        self.last_gpu_at = 0.0

    def emit(self, stage: str, *, processed: float | None = None, duration: float | None = None,
             eta: float | None = None, speed: float | None = None, reliable: bool = False) -> None:
        if not self.callback:
            return
        now = perf_counter()
        if self.device == "cuda" and now - self.last_gpu_at >= 2.5:
            self.gpu = gpu_info()
            self.last_gpu_at = now
        self.callback(SpeechProgress(
            stage, self.engine, self.model, self.device, self.compute, now - self.start,
            processed, duration, eta, speed, self.gpu if self.device == "cuda" else None, reliable,
        ))


def transcript_cache_path(media_path: Path, model: str, language: str | None = None, *, identity: str = "") -> Path:
    stat = media_path.stat()
    digest = hashlib.sha256(
        f"v3:{media_path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}:{model}:{language or 'auto'}:{identity}".encode()
    )
    with media_path.open("rb") as source:
        digest.update(source.read(65536))
        if stat.st_size > 65536:
            source.seek(max(65536, stat.st_size - 65536))
            digest.update(source.read(65536))
    return CACHE_DIR / "transcripts" / f"{digest.hexdigest()[:32]}.json"


def _cache(path: Path, transcript: Transcript) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False)
    pending = Path(stream.name)
    try:
        with stream:
            json.dump(transcript.to_dict(), stream, ensure_ascii=False)
        pending.replace(path)
    finally:
        pending.unlink(missing_ok=True)


def _get_cache(path: Path) -> Transcript | None:
    try:
        result = Transcript.from_dict(json.loads(path.read_text(encoding="utf-8")))
        return result if result.segments else None
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def _audio_timing(source: Path, manager: ProcessManager) -> tuple[float, float | None, float | None]:
    info = probe(source, manager)
    streams = info.get("streams", [])
    audio = next((stream for stream in streams if stream["codec_type"] == "audio"), None)
    video = next((stream for stream in streams if stream["codec_type"] == "video"), None)
    if audio is None:
        raise RuntimeError("The source video does not contain an audio stream.")
    container = info.get("format", {})
    video_start = _number((video or {}).get("start_time"))
    if video_start is None:
        video_start = _number(container.get("start_time"))
    audio_start = _number(audio.get("start_time"))
    offset = audio_start - video_start if audio_start is not None and video_start is not None else 0.0
    return offset, _number(audio.get("duration")), _number((video or {}).get("duration") or container.get("duration"))


def _number(value: object) -> float | None:
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _batch_size(requested: int, device: str, gpu: GpuInfo | None, model: str) -> int:
    # Sequential inference is the validated default. Cap manual batch sizes before attempting CUDA.
    if requested <= 1 or device != "cuda" or gpu is None:
        return 1
    large = model.startswith(("large", "medium"))
    if gpu.free_mib < (4500 if large else 3000):
        return 1
    return min(requested, 2 if gpu.free_mib < 5500 else 4 if gpu.free_mib < 8000 else 8)


def transcribe(media_path: Path, model_name: str, device_preference: str,
               language: str | None, cache: bool, manager: ProcessManager,
               log: Callable[[str], None], *, settings: SpeechSettings | None = None,
               progress: Callable[[SpeechProgress], None] | None = None,
               profile_limits: ProfileLimits | None = None) -> Transcript:
    settings = settings or SpeechSettings()
    language = language or settings.language or None
    reporter = _Reporter(model_name, progress)
    reporter.emit("Checking speech engine")
    manager.check_cancelled()
    if sys.platform == "darwin" and device_preference != "cpu" and settings.engine in {"auto", "mlx"} and mlx_whisper_available():
        repo = MLX_REPOS.get(model_name, f"mlx-community/whisper-{model_name}-mlx")
        reporter.engine = "MLX Whisper (Apple Silicon)"
        reporter.device = "mlx"
        reporter.compute = "float16"
        try:
            mlx_id = f"mlx-whisper:{version('mlx-whisper')}:{repo}:{(settings.vad, settings.word_timestamps)}"
            cache_path = transcript_cache_path(media_path, model_name, language, identity=mlx_id) if cache else None
            if cache_path is not None and (cached := _get_cache(cache_path)):
                log(f"Using cached MLX transcript ({len(cached.segments)} segments)")
                reporter.emit("Using cached transcript")
                return cached
            offset, audio_duration, video_duration = _audio_timing(media_path, manager)
            log(f"Loading MLX Whisper {model_name} on Apple Silicon ({repo})")
            reporter.emit("Loading model", duration=audio_duration)
            entries, detected, duration = _run_mlx_whisper(repo, media_path, language, settings, manager, reporter)
            transcript = _validated_transcript(entries, detected, duration, offset, video_duration, log)
            if cache_path is not None:
                _cache(cache_path, transcript)
            reporter.emit("Transcription complete", processed=duration, duration=duration, reliable=True)
            return transcript
        except CancelledError:
            raise
        except Exception as error:
            if settings.engine == "mlx" or not settings.cpu_fallback:
                raise
            log(f"MLX Whisper unavailable or failed ({error}); falling back to Python Faster-Whisper CPU")

    gpu = gpu_info() if device_preference != "cpu" else None
    executable = find_executable(settings.executable) if settings.engine != "python" else None
    model_path = find_model(model_name, settings, executable)
    xxl = None
    if executable and (settings.engine == "purfview" or (model_path and device_preference != "cpu")):
        try:
            xxl = inspect_purfview(executable)
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
            if settings.engine == "purfview":
                raise
            log(f"Purfview unavailable; using Python Faster-Whisper ({error})")
    if settings.engine == "purfview" and xxl is None:
        raise RuntimeError("Purfview XXL was not found. Select its executable in Speech-to-Text settings.")
    if settings.engine == "auto" and xxl and not xxl.cuda:
        xxl = None
    cuda = (xxl.cuda if xxl else _python_cuda()) if device_preference != "cpu" else False
    if device_preference == "cuda" and not cuda:
        if not settings.cpu_fallback:
            raise RuntimeError("CUDA is unavailable for the selected engine. Check its runtime libraries or allow CPU fallback.")
        log("CUDA is unavailable for the selected engine; using CPU")
    device = "cuda" if cuda else "cpu"
    reporter.engine = "Purfview Faster-Whisper-XXL" if xxl else "Python Faster-Whisper"
    reporter.device = device
    reporter.gpu = gpu
    if xxl and model_path is None:
        reporter.emit("Downloading model weights")
        model_path = download_speech_model(model_name, settings)
    if xxl:
        engine_id = xxl.identity
    else:
        import ctranslate2
        engine_id = f"faster-whisper:{version('faster-whisper')}:ctranslate2:{ctranslate2.__version__}"
    if model_path:
        model_stat = (model_path / "model.bin").stat()
        engine_id += f":{model_path}:{model_stat.st_size}:{model_stat.st_mtime_ns}"
    relevant_settings = (settings.vad, settings.word_timestamps)
    requested_batch = settings.batch_size if settings.batch_size > 0 else (
        profile_limits.whisper_batch_size if profile_limits is not None else 0
    )
    cpu_threads = profile_limits.cpu_threads if profile_limits is not None else 0
    primary_compute = choose_compute(device, settings.compute_type, gpu)
    primary_batch = _batch_size(requested_batch, device, gpu, model_name)
    attempts = [(device, primary_compute, primary_batch)]
    if xxl and not {"--batched", "--batch_size"} <= xxl.flags and attempts[0][2] > 1:
        log("This Purfview build does not support batch mode; using sequential inference")
        attempts[0] = (device, attempts[0][1], 1)
    if device == "cuda":
        if attempts[0][2] > 2:
            attempts.append(("cuda", primary_compute, max(1, attempts[0][2] // 2)))
        if attempts[0][1:] != ("int8_float16", 1):
            attempts.append(("cuda", "int8_float16", 1))
        if settings.cpu_fallback:
            attempts.append(("cpu", "int8", 1))

    timing = None
    for index, (device, compute, batch) in enumerate(attempts):
        manager.check_cancelled()
        reporter.device, reporter.compute = device, compute
        identity = json.dumps([engine_id, device, compute, batch, relevant_settings])
        cache_path = transcript_cache_path(media_path, model_name, language, identity=identity) if cache else None
        if cache_path is not None and (cached := _get_cache(cache_path)):
            log(f"Using cached transcript ({len(cached.segments)} segments)")
            reporter.emit("Using cached transcript")
            return cached
        if timing is None:
            timing = _audio_timing(media_path, manager)
        offset, audio_duration, video_duration = timing
        log(f"Loading {reporter.engine} {model_name} on {device.upper()} ({compute}, batch {batch})")
        reporter.emit("Loading model", duration=audio_duration)
        try:
            if xxl and model_path:
                entries, detected, duration = _run_purfview(
                    xxl, model_path, model_name, media_path, device, compute, batch, language,
                    settings, manager, log, reporter, audio_duration,
                )
            else:
                if cpu_threads > 0:
                    entries, detected, duration = _run_python(
                        model_path or model_name, media_path, device, compute, batch, language,
                        settings, manager, reporter, cpu_threads=cpu_threads,
                    )
                else:
                    entries, detected, duration = _run_python(
                        model_path or model_name, media_path, device, compute, batch, language,
                        settings, manager, reporter,
                    )
        except CancelledError:
            raise
        except Exception as error:
            manager.check_cancelled()
            if index + 1 == len(attempts) or not _CUDA_ERROR.search(str(error)):
                raise
            next_device, next_compute, next_batch = attempts[index + 1]
            log(f"{device.upper()} transcription failed ({error}); retrying on {next_device.upper()} "
                f"({next_compute}, batch {next_batch})")
            continue
        transcript = _validated_transcript(entries, detected, duration, offset, video_duration, log)
        manager.check_cancelled()
        if cache_path is not None:
            if model_path is None:
                downloaded_model = find_model(model_name, settings, executable)
                if downloaded_model:
                    stat = (downloaded_model / "model.bin").stat()
                    new_id = f"{engine_id}:{downloaded_model}:{stat.st_size}:{stat.st_mtime_ns}"
                    cache_path = transcript_cache_path(media_path, model_name, language,
                        identity=json.dumps([new_id, device, compute, batch, relevant_settings]))
            _cache(cache_path, transcript)
        reporter.emit("Transcription complete", processed=duration, duration=duration, reliable=True)
        return transcript
    raise RuntimeError("No transcription configuration could run.")


def _run_python(model_name: str | Path, media_path: Path, device: str, compute: str, batch: int,
                language: str | None, settings: SpeechSettings, manager: ProcessManager,
                reporter: _Reporter, *, cpu_threads: int = 0) -> tuple[list[dict], str, float]:
    from faster_whisper import BatchedInferencePipeline, WhisperModel
    model = WhisperModel(
        str(model_name), device=device, compute_type=compute,
        cpu_threads=max(0, cpu_threads), download_root=str(model_cache_root()),
    )
    iterator = None
    try:
        manager.check_cancelled()
        reporter.emit("Decoding audio and detecting speech")
        shared = dict(language=language, task="transcribe", beam_size=5, vad_filter=settings.vad,
                      word_timestamps=settings.word_timestamps, condition_on_previous_text=False)
        if batch > 1:
            runner = BatchedInferencePipeline(model=model)
            iterator, info = runner.transcribe(str(media_path), batch_size=batch, **shared)
        else:
            iterator, info = model.transcribe(str(media_path), **shared)
        duration = float(info.duration)
        results = []
        started = perf_counter()
        for item in iterator:
            manager.check_cancelled()
            results.append({
                "start": item.start, "end": item.end, "text": item.text, "no_speech_prob": item.no_speech_prob,
                "avg_logprob": item.avg_logprob,
                "words": [{"start": word.start, "end": word.end, "word": word.word} for word in item.words or []],
            })
            speed = item.end / max(0.001, perf_counter() - started)
            reporter.emit("Transcribing speech", processed=item.end, duration=duration, speed=speed)
        return results, str(info.language), duration
    finally:
        if iterator is not None:
            iterator.close()
        model.model.unload_model()
        del model
        gc.collect()


def _run_mlx_whisper(repo: str, media_path: Path, language: str | None, settings: SpeechSettings,
                     manager: ProcessManager, reporter: _Reporter) -> tuple[list[dict], str, float]:
    import mlx_whisper

    reporter.emit("Transcribing speech on Apple Silicon")
    manager.check_cancelled()
    try:
        kwargs: dict[str, object] = {
            "path_or_hf_repo": repo,
            "word_timestamps": settings.word_timestamps,
        }
        if language:
            kwargs["language"] = language
        with _MLX_INFERENCE_LOCK:
            result = mlx_whisper.transcribe(str(media_path), **kwargs)
        manager.check_cancelled()
        segments = result.get("segments") or []
        entries = []
        last_end = 0.0
        for item in segments:
            end = float(item.get("end") or 0.0)
            last_end = max(last_end, end)
            entries.append({
                "start": item.get("start"),
                "end": item.get("end"),
                "text": item.get("text", ""),
                "no_speech_prob": item.get("no_speech_prob"),
                "avg_logprob": item.get("avg_logprob"),
                "words": [
                    {"start": w.get("start"), "end": w.get("end"), "word": w.get("word", "")}
                    for w in (item.get("words") or [])
                ],
            })
        return entries, str(result.get("language") or language or "unknown"), last_end
    finally:
        release_mlx_memory()


def _run_purfview(engine: Purfview, model: Path, model_name: str, media: Path, device: str, compute: str,
                  batch: int, language: str | None, settings: SpeechSettings, manager: ProcessManager,
                  log: Callable[[str], None], reporter: _Reporter, audio_duration: float | None
                  ) -> tuple[list[dict], str, float]:
    with TemporaryDirectory(prefix="autoclip-xxl-") as directory:
        folder = Path(directory)
        models = stage_model(model, folder, model_name)
        command = [
            engine.executable, media.resolve(), "--model", model_name, "--model_dir", models, "--device", device,
            "--compute_type", compute, "--output_dir", folder, "--task", "transcribe", "--beam_size", "5",
            "--condition_on_previous_text", "False", "--vad_filter", str(settings.vad),
            "--word_timestamps", str(settings.word_timestamps),
        ]
        if language:
            command += ["--language", language]
        if batch > 1:
            command += ["--batched", "--batch_size", str(batch)]
        command.extend(flag for flag in ("--beep_off", "--print_progress") if flag in engine.flags)
        command += ["--output_format", "json"]
        reported_duration = None

        def output(line: str) -> None:
            nonlocal reported_duration
            log(line)
            match = _XXL_PROGRESS.match(line)
            if match:
                processed, total, _elapsed, eta, speed = match.groups()
                reported_duration = float(total)
                eta_seconds = 0
                for component in eta.split(":"):
                    eta_seconds = 60 * eta_seconds + int(component)
                reporter.emit("Transcribing speech", processed=float(processed), duration=float(total),
                              eta=eta_seconds, speed=float(speed), reliable=True)
            elif "inference" in line.lower():
                reporter.emit("Starting inference", duration=audio_duration)

        try:
            result = manager.run(
                command, cwd=folder, on_output=output, output_tail=18,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "HF_HUB_OFFLINE": "1",
                     "HF_HOME": str(folder / "hub"), "HF_HUB_CACHE": str(folder / "hub")},
            )
        except subprocess.CalledProcessError as error:
            raise RuntimeError(f"Purfview failed (exit code {error.returncode}).\n{error.output or ''}") from error
        transcript_path = folder / (media.stem + ".json")
        if not transcript_path.is_file():
            raise RuntimeError(f"Purfview produced no JSON transcript.\n{result.stdout}")
        value = json.loads(transcript_path.read_text(encoding="utf-8-sig"))
        entries = value["segments"]
        last_segment = max((_number(item.get("end")) or 0 for item in entries), default=0)
        # The engine reports decoded sample time, which can differ from stream metadata.
        duration = max(last_segment, reported_duration or audio_duration or 0)
        return entries, str(value.get("language") or language or "unknown"), duration


def _validated_transcript(entries: list[dict], language: str, duration: float, offset: float,
                          video_duration: float | None, log: Callable[[str], None]) -> Transcript:
    limit = max(0.0, duration + offset)
    if video_duration and limit > video_duration:
        limit = video_duration
    segments = []
    end_of_previous = 0.0
    revised = flagged = 0
    previous_text = ""
    repetition = 0
    for item in entries:
        start, end = _number(item.get("start")), _number(item.get("end"))
        text = str(item.get("text") or "").strip()
        if start is None or end is None or end <= start or not text:
            revised += 1
            continue
        start, end = start + offset, end + offset
        bounded_start, bounded_end = max(end_of_previous, start, 0.0), min(end, limit)
        revised += (bounded_start, bounded_end) != (start, end)
        if bounded_end <= bounded_start:
            continue
        words = []
        cursor = bounded_start
        for word in item.get("words") or []:
            word_text = str(word.get("word") or "").strip()
            if not word_text:
                continue
            word_start, word_end = _number(word.get("start")), _number(word.get("end"))
            left = min(bounded_end, max(cursor, word_start + offset if word_start is not None else cursor))
            right = min(bounded_end, max(left, word_end + offset if word_end is not None else left))
            words.append(Word(left, right, word_text))
            cursor = right
        signature = " ".join(re.findall(r"\w+", text.casefold()))
        repetition = repetition + 1 if signature == previous_text else 1
        previous_text = signature
        low_confidence = (_number(item.get("avg_logprob")) or 0) < -0.8
        quiet = (_number(item.get("no_speech_prob")) or 0) >= 0.8
        loop = re.search(r"\b(\w+)(?: \1){5,}\b", signature) is not None or repetition >= 3
        if low_confidence and (quiet or loop):
            flagged += 1
        segments.append(Segment(bounded_start, bounded_end, text, words))
        end_of_previous = bounded_end
    if not segments:
        raise RuntimeError("No speech was detected in the source video.")
    if revised:
        log(f"Validated speech timing and adjusted or discarded {revised} out-of-range segments")
    if flagged:
        log(f"Review {flagged} low-confidence speech segments near silence or repetition; their text was retained")
    return Transcript(limit, language, segments)


def export_transcript(transcript: Transcript, directory: Path) -> None:
    _cache(directory / "transcript.json", transcript)

    def timestamp(value: float) -> str:
        seconds, ms = divmod(round(value * 1000), 1000)
        minutes, seconds = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        return f"{hours:02}:{minutes:02}:{seconds:02},{ms:03}"

    payload = "\n\n".join(f"{index}\n{timestamp(segment.start)} --> {timestamp(segment.end)}\n{segment.text}"
                          for index, segment in enumerate(transcript.segments, 1))
    (directory / "transcript.srt").write_text(payload + "\n", encoding="utf-8-sig")
