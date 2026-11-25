from __future__ import annotations

import json
import re
import shutil
import traceback
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from .config import SettingsStore
from .database import Database
from .highlights import select_highlights
from .media import acquire_source
from .models import ProjectOptions, RenderedClip
from .paths import CACHE_DIR, OUTPUT_DIR
from .process import CancelledError, ProcessManager
from .providers import AIProvider, ProviderError
from .rendering import render_clip
from .transcription import transcribe


Progress = Callable[[int, str], None]


class Pipeline:
    def __init__(self, database: Database, settings: SettingsStore,
                 manager: ProcessManager | None = None) -> None:
        self.database = database
        self.settings = settings
        self.manager = manager or ProcessManager()

    def run(self, project_id: str, options: ProjectOptions, progress: Progress) -> list[RenderedClip]:
        self.manager.reset()
        self.database.update_project(project_id, status="processing", progress=1, error="")
        job_dir = CACHE_DIR / "jobs" / project_id
        job_dir.mkdir(parents=True, exist_ok=True)
        successful = False
        try:
            self._update(project_id, 3, "Acquiring source video", progress)
            source = acquire_source(options.source, options.cache_enabled, self.manager, self._logger(progress, 4))
            self._update(project_id, 12, "Generating timestamped transcript", progress)
            transcript = transcribe(
                source, options.whisper_model, options.whisper_device, options.language,
                options.cache_enabled, self.manager, self._logger(progress, 13),
            )
            self.manager.check_cancelled()
            self._update(project_id, 40, "Selecting the strongest highlights", progress)
            provider = self._provider(options.provider, options.model)
            highlights = select_highlights(
                transcript, options.clip_count, options.min_duration, options.max_duration,
                provider, self._logger(progress, 42), source, options.multimodal,
            )
            if not highlights:
                raise RuntimeError("No usable highlights were found. Try shorter minimum duration or another video.")
            project_output = self._output_root(options) / project_id
            project_output.mkdir(parents=True, exist_ok=True)
            rendered: list[RenderedClip] = []
            for index, highlight in enumerate(highlights, 1):
                self.manager.check_cancelled()
                start_progress = 48 + round((index - 1) / len(highlights) * 47)
                self._update(project_id, start_progress, f"Rendering clip {index}/{len(highlights)}", progress)
                slug = _slug(highlight.title) or f"clip-{index}"
                output = project_output / f"{index:02d}-{slug}.mp4"
                duration = render_clip(
                    source, highlight, transcript, output, job_dir / f"clip-{index}",
                    options.output_size, options.aspect_ratio, options.face_tracking,
                    options.captions, options.caption_template,
                    bool(self.settings.data.get("hardware_encoding", True)), self.manager,
                    self._logger(progress, start_progress),
                )
                rendered_clip = RenderedClip(output, highlight, duration)
                rendered.append(rendered_clip)
                self.database.add_clip(
                    project_id, str(output), highlight.title, highlight.hook,
                    highlight.description, highlight.score, duration,
                )
            manifest = {
                "project_id": project_id, "source": str(source),
                "options": asdict(options),
                "clips": [{"path": str(item.path), "duration": item.duration,
                           "highlight": asdict(item.highlight)} for item in rendered],
            }
            (project_output / "project.json").write_text(
                json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            self._update(project_id, 100, f"Completed {len(rendered)} clips", progress)
            self.database.update_project(project_id, status="completed", progress=100)
            successful = True
            return rendered
        except CancelledError:
            self.database.update_project(project_id, status="cancelled", error="Cancelled by user")
            progress(0, "Processing cancelled")
            raise
        except Exception as error:
            self.database.update_project(project_id, status="failed", error=str(error))
            progress(0, f"Failed: {error}")
            raise
        finally:
            if successful:
                shutil.rmtree(job_dir, ignore_errors=True)

    def _provider(self, name: str, model: str) -> AIProvider | None:
        values = self.settings.provider(name)
        key = self.settings.api_key(name)
        if not key:
            return None
        return AIProvider(name, key, values.get("base_url", ""), model or values.get("model", ""))

    def _output_root(self, options: ProjectOptions) -> Path:
        configured = options.output_dir or self.settings.data.get("output_dir")
        return Path(configured).expanduser() if configured else OUTPUT_DIR

    def _update(self, project_id: str, value: int, message: str, callback: Progress) -> None:
        self.database.update_project(project_id, progress=value)
        callback(value, message)

    @staticmethod
    def _logger(callback: Progress, value: int) -> Callable[[str], None]:
        return lambda message: callback(value, message)


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:55]
