from __future__ import annotations

import json
import re
import shutil
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from .config import SettingsStore
from .database import Database
from .diagnostics import LOG, SUCCESS, log_error, redact
from .heatmap import HeatmapPoint, parse_heatmap
from .highlights import select_highlights
from .media import DownloadProgress, acquire_source, is_youtube_url, probe, youtube_id
from .models import ProjectOptions, RenderedClip, Transcript
from .paths import CACHE_DIR, OUTPUT_DIR
from .performance import HardwareMonitor, resolve_profile_limits
from .process import CancelledError, ProcessManager
from .providers import AIProvider, ProviderError
from .rendering import render_clip, source_dimensions
from .subtitles import parse_srt_file
from .thumbnails import generate_clip_thumbnail
from .transcription import SpeechProgress, export_transcript, transcribe
from .youtube_metadata import YouTubeMetadata, YouTubeMetadataError, fetch_youtube_metadata
from .youtube_subtitles import fetch_youtube_transcript


Progress = Callable[[int, str], None]


class Pipeline:
    def __init__(self, database: Database, settings: SettingsStore,
                 manager: ProcessManager | None = None,
                 monitor: HardwareMonitor | None = None) -> None:
        self.database = database
        self.settings = settings
        self.manager = manager or ProcessManager()
        self.monitor = monitor or HardwareMonitor()

    def run(self, project_id: str, options: ProjectOptions, progress: Progress, *,
            resume_analysis: bool = False, warning: Callable[[str], None] | None = None,
            download_progress: Callable[[DownloadProgress], None] | None = None,
            download_log: Callable[[str], None] | None = None,
            speech_progress: Callable[[SpeechProgress], None] | None = None,
            speech_log: Callable[[str], None] | None = None) -> list[RenderedClip]:
        if resume_analysis:
            if not self.database.claim_analysis_retry(project_id, asdict(options)):
                raise RuntimeError("This project is no longer waiting for an AI analysis retry.")
        else:
            self.database.update_project(project_id, status="processing", progress=1, error="")
        LOG.info("Project %s %s", project_id, "retrying AI analysis" if resume_analysis else "started")
        job_dir = CACHE_DIR / "jobs" / project_id
        checkpoint = job_dir / "analysis.json"
        keep_job = False
        analysis_pending = resume_analysis
        try:
            speech = self.settings.speech()
            perf = self.settings.performance()
            snap = self.monitor.snapshot(perf.gaming_process if perf.gaming_mode else "")
            limits = resolve_profile_limits(perf.profile, snap)
            self.manager.default_creationflags = limits.subprocess_priority_flags
            LOG.info(
                "Performance profile=%s (cpu_threads=%s, ffmpeg_threads=%s, batch=%s, track_interval=%.2fs)",
                limits.profile, limits.cpu_threads, limits.ffmpeg_threads,
                limits.whisper_batch_size, limits.tracking_sample_interval,
            )
            job_dir.mkdir(parents=True, exist_ok=True)
            if resume_analysis:
                source, dimensions, transcript = _load_analysis(checkpoint)
                yt_meta_obj = _metadata_from_options(options)
            else:
                youtube_source = is_youtube_url(options.source)
                yt_meta_obj = _metadata_from_options(options)
                vid_id = youtube_id(options.source) if youtube_source else None
                if youtube_source and vid_id and yt_meta_obj is None and (
                    options.highlight_mode in {"hybrid", "most_replayed"} or options.use_youtube_subtitles
                ):
                    try:
                        yt_meta_obj, _ = fetch_youtube_metadata(vid_id, self.manager)
                    except YouTubeMetadataError as meta_err:
                        LOG.warning("YouTube metadata unavailable for %s: %s", vid_id, meta_err)
                self._update(
                    project_id, 3,
                    "Downloading YouTube video" if youtube_source else "Importing local video (download skipped)",
                    progress,
                )
                source = acquire_source(
                    options.source, options.cache_enabled, self.manager,
                    download_log or self._logger(progress, 4), job_dir=job_dir, progress=download_progress,
                )
                dimensions = source_dimensions(source, self.manager)
                self.monitor.wait_if_constrained(perf, self.manager.cancel_event, self._logger(progress, 11))
                # Transcript priority: 1) User SRT -> 2) YouTube subtitles -> 3) Local/Cloud Speech-to-Text
                transcript = None
                if options.srt_path:
                    self._update(project_id, 12, "Importing external SRT subtitles (transcription skipped)", progress)
                    info = probe(source, self.manager)
                    vid_dur = float(info.get("format", {}).get("duration") or 0.0)
                    srt_result = parse_srt_file(
                        Path(options.srt_path),
                        offset_seconds=options.srt_offset_seconds,
                        fallback_duration=vid_dur,
                    )
                    transcript = srt_result.transcript
                    LOG.info(
                        "Imported SRT %s (%s cues, lang=%s, encoding=%s)",
                        Path(options.srt_path).name, srt_result.cue_count,
                        srt_result.detected_language, srt_result.encoding,
                    )
                elif youtube_source and vid_id and options.use_youtube_subtitles and yt_meta_obj is not None:
                    self._update(project_id, 12, "Checking YouTube subtitles / automatic captions", progress)
                    info = probe(source, self.manager)
                    vid_dur = float(info.get("format", {}).get("duration") or yt_meta_obj.duration or 0.0)
                    yt_sub = fetch_youtube_transcript(
                        vid_id,
                        self.manager,
                        metadata=yt_meta_obj,
                        preferred_language=options.youtube_subtitle_language or options.language or "",
                        video_duration=vid_dur,
                        cache_enabled=options.cache_enabled,
                        log=self._logger(progress, 12),
                    )
                    if yt_sub is not None:
                        transcript, sub_label = yt_sub
                        self._update(project_id, 28, f"Using {sub_label} (transcription skipped)", progress)
                if transcript is None:
                    self._update(project_id, 12, "Generating timestamped transcript", progress)
                    transcript = transcribe(
                        source, options.whisper_model, options.whisper_device, options.language,
                        options.cache_enabled, self.manager, speech_log or self._logger(progress, 13),
                        settings=speech, progress=speech_progress, profile_limits=limits,
                    )
                self._update(project_id, 40, "Selecting the strongest highlights", progress)
            self.manager.check_cancelled()
            self.monitor.wait_if_constrained(perf, self.manager.cancel_event, self._logger(progress, 40))
            heatmap_points: list[HeatmapPoint] = (
                parse_heatmap(yt_meta_obj.heatmap, duration=transcript.duration)
                if yt_meta_obj is not None and yt_meta_obj.heatmap else []
            )
            effective_mode = options.highlight_mode
            if not is_youtube_url(options.source) and effective_mode in {"hybrid", "most_replayed"}:
                self._logger(progress, 40)("Local video selected; using AI Analysis (YouTube heatmap unavailable).")
                effective_mode = "ai"
            elif effective_mode in {"hybrid", "most_replayed"} and not heatmap_points:
                self._logger(progress, 40)("No YouTube heatmap data available; falling back to AI Analysis.")
                effective_mode = "ai"
            try:
                provider = self._provider(options.provider, options.model, warning)
                if resume_analysis:
                    if provider is None:
                        raise ProviderError("No API key is configured. Save a key in AI Provider Settings, then retry.",
                                            category="credentials")
                    progress(40, redact(
                        f"Retrying AI analysis with {provider.kind} model {provider.model} "
                        "using the prepared video and transcript", provider.api_key,
                    ))
                highlights = select_highlights(
                    transcript, options.clip_count, options.min_duration, options.max_duration,
                    provider, self._logger(progress, 40), source, options.multimodal,
                    manager=self.manager, cache_enabled=options.cache_enabled,
                    mode=effective_mode, heatmap_points=heatmap_points, ai_weight=options.ai_weight,
                )
            except ProviderError as error:
                self.manager.check_cancelled()
                if not resume_analysis:
                    _save_analysis(checkpoint, source, dimensions, transcript)
                keep_job = True
                message = redact(str(error))
                self.database.update_project(project_id, status="analysis_failed", progress=40, error=message)
                LOG.error("Project %s AI analysis failed\n%s", project_id, message)
                raise
            self.manager.check_cancelled()
            analysis_pending = False
            if not highlights:
                raise RuntimeError("No usable highlights were found. Try shorter minimum duration or another video.")
            project_output = self._output_root(options) / project_id
            project_output.mkdir(parents=True, exist_ok=True)
            if speech.export_srt:
                export_transcript(transcript, project_output)
            rendered: list[RenderedClip] = []
            encoder_support: dict[str, bool] = {}
            hardware_encoding = bool(self.settings.data.get("hardware_encoding", True))
            for index, highlight in enumerate(highlights, 1):
                self.manager.check_cancelled()
                self.monitor.wait_if_constrained(perf, self.manager.cancel_event, self._logger(progress, 48))
                start_progress = 48 + round((index - 1) / len(highlights) * 47)
                self._update(project_id, start_progress, f"Rendering clip {index}/{len(highlights)}", progress)
                slug = _slug(highlight.title) or f"clip-{index}"
                output = project_output / f"{index:02d}-{slug}.mp4"
                duration = render_clip(
                    source, highlight, transcript, output, job_dir / f"clip-{index}",
                    options.output_size, options.aspect_ratio, options.face_tracking,
                    options.captions, options.caption_template,
                    hardware_encoding, self.manager, self._logger(progress, start_progress),
                    dimensions=dimensions, encoder_support=encoder_support,
                    profile_limits=limits, cache_enabled=options.cache_enabled,
                    reframe_mode=options.reframe_mode if options.face_tracking else "off",
                )
                rendered_clip = RenderedClip(output, highlight, duration)
                rendered.append(rendered_clip)
                thumb_path = thumb_raw = thumb_key = ""
                thumb_ts: float | None = None
                try:
                    thumb = generate_clip_thumbnail(
                        output, project_output / "thumbnails",
                        title=highlight.title, profile=limits.profile,
                    )
                    thumb_path = str(thumb.preview_path)
                    thumb_raw = str(thumb.raw_path)
                    thumb_ts = thumb.timestamp
                    thumb_key = thumb.cache_key
                except Exception as thumb_error:
                    LOG.warning("Thumbnail generation failed for %s: %s", output.name, thumb_error)
                self.database.add_clip(
                    project_id, str(output), highlight.title, highlight.hook,
                    highlight.description, highlight.score, duration,
                    thumbnail_path=thumb_path, thumbnail_raw_path=thumb_raw,
                    thumbnail_timestamp=thumb_ts, thumbnail_cache_key=thumb_key,
                    ai_score=highlight.ai_score, replay_score=highlight.replay_score,
                    reason=highlight.reason, detection_mode=highlight.detection_mode,
                )
            manifest = {
                "project_id": project_id,
                "source": options.source if source.is_relative_to(job_dir.resolve()) else str(source),
                "options": asdict(options),
                "clips": [{"path": str(item.path), "duration": item.duration,
                           "highlight": asdict(item.highlight)} for item in rendered],
            }
            self.manager.check_cancelled()
            pending_manifest = project_output / "project.json.tmp"
            try:
                pending_manifest.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
                pending_manifest.replace(project_output / "project.json")
            finally:
                pending_manifest.unlink(missing_ok=True)
            self._update(project_id, 100, f"Completed {len(rendered)} clips", progress)
            self.database.update_project(project_id, status="completed", progress=100)
            LOG.log(SUCCESS, "Project %s completed with %s clips", project_id, len(rendered))
            return rendered
        except CancelledError:
            if analysis_pending and checkpoint.exists():
                keep_job = True
                self.database.update_project(project_id, status="analysis_failed", error="AI analysis retry cancelled")
            else:
                self.database.update_project(project_id, status="cancelled", error="Cancelled by user")
            raise
        except Exception as error:
            if keep_job:
                raise
            if self.manager.cancel_event.is_set():
                self.database.update_project(project_id, status="cancelled", error="Cancelled by user")
                raise CancelledError("Processing cancelled") from error
            log_error(f"Project {project_id} failed", error)
            self.database.update_project(project_id, status="failed", error=redact(str(error)))
            raise
        finally:
            if not keep_job:
                shutil.rmtree(job_dir, ignore_errors=True)

    def _provider(self, name: str, model: str, warning: Callable[[str], None] | None) -> AIProvider | None:
        resolved = self.settings.resolve_provider_config(name, model_override=model)
        if not resolved["api_key"]:
            return None
        return AIProvider(
            resolved["provider"], resolved["api_key"], resolved["base_url"], resolved["model"],
            fallback_models=tuple(resolved["fallback_models"]), auto_fallback=bool(resolved["auto_fallback"]),
            cancel_event=self.manager.cancel_event, notify=warning,
        )

    def _output_root(self, options: ProjectOptions) -> Path:
        configured = options.output_dir or self.settings.data.get("output_dir")
        return Path(configured).expanduser() if configured else OUTPUT_DIR

    def _update(self, project_id: str, value: int, message: str, callback: Progress) -> None:
        LOG.info("Project %s: %s", project_id, message)
        self.database.update_project(project_id, progress=value)
        callback(value, message)

    @staticmethod
    def _logger(callback: Progress, value: int) -> Callable[[str], None]:
        def emit(message: str) -> None:
            if message.startswith(("Loading Faster-Whisper", "Rendering with")):
                LOG.info("%s", message)
            callback(value, message)
        return emit


def _metadata_from_options(options: ProjectOptions) -> YouTubeMetadata | None:
    raw = options.youtube_metadata
    if not isinstance(raw, dict) or not raw.get("video_id"):
        return None
    try:
        return YouTubeMetadata(
            video_id=str(raw["video_id"]),
            title=str(raw.get("title") or ""),
            channel=str(raw.get("channel") or ""),
            duration=int(raw["duration"]) if raw.get("duration") is not None else None,
            resolution=str(raw.get("resolution") or ""),
            view_count=int(raw["view_count"]) if raw.get("view_count") is not None else None,
            upload_date=str(raw["upload_date"]) if raw.get("upload_date") else None,
            heatmap=tuple(raw.get("heatmap") or ()),
            subtitle_languages=tuple(raw.get("subtitle_languages") or ()),
            auto_caption_languages=tuple(raw.get("auto_caption_languages") or ()),
            language=str(raw.get("language") or ""),
        )
    except (TypeError, ValueError, KeyError):
        return None


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:55]


def _save_analysis(path: Path, source: Path, dimensions: tuple[int, int], transcript: Transcript) -> None:
    pending = path.with_suffix(".tmp")
    try:
        with pending.open("w", encoding="utf-8") as stream:
            json.dump({"source": str(source.resolve()), "dimensions": dimensions, "transcript": transcript.to_dict()}, stream)
        pending.replace(path)
    finally:
        pending.unlink(missing_ok=True)


def _load_analysis(path: Path) -> tuple[Path, tuple[int, int], Transcript]:
    try:
        with path.open(encoding="utf-8") as stream:
            state = json.load(stream)
        source = Path(state["source"])
        width, height = state["dimensions"]
        transcript = Transcript.from_dict(state["transcript"])
        if source.is_file() and transcript.segments and width > 0 and height > 0:
            return source, (width, height), transcript
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    raise RuntimeError("The prepared video or transcript is unavailable. Start a new project to prepare it again.")
