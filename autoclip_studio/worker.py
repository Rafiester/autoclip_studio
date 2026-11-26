from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QObject, QThread, Signal, Slot

from .diagnostics import log_error, redact
from .media import DownloadError
from .models import ProjectOptions
from .pipeline import Pipeline
from .process import CancelledError, PausedError
from .providers import ProviderError
from .youtube_metadata import YouTubeMetadataError


def _format_exception_message(error: Exception) -> str:
    import subprocess
    if isinstance(error, subprocess.CalledProcessError):
        tail = redact((error.output or error.stderr or "").strip())
        if tail:
            first_err = next((ln.strip() for ln in reversed(tail.splitlines()) if ln.strip()), "")
            return f"Subprocess failed (exit code {error.returncode}): {first_err}\n\nTechnical details:\n{tail}"
    return redact(str(error))


class FunctionThread(QThread):
    succeeded = Signal(object)
    failed = Signal(str)
    warning = Signal(str)

    def __init__(self, function: Callable[[], object], parent: QObject, *,
                 operation: str = "Provider operation") -> None:
        super().__init__(parent)
        self.function = function
        self.operation = operation

    def run(self) -> None:
        try:
            self.succeeded.emit(self.function())
        except CancelledError:
            return
        except (ProviderError, YouTubeMetadataError, DownloadError, RuntimeError, ValueError) as error:
            log_error(f"{self.operation} failed", error)
            self.failed.emit(redact(str(error)))
        except Exception as error:
            log_error(f"{self.operation} failed", error)
            self.failed.emit(f"{self.operation} failed ({type(error).__name__}).")


class PipelineWorker(QObject):
    progress = Signal(int, str)
    download_progress = Signal(object)
    download_log = Signal(str)
    speech_progress = Signal(object)
    speech_log = Signal(str)
    warning = Signal(str)
    completed = Signal(str, int)
    paused = Signal(str, str)
    failed = Signal(str, str)
    finished = Signal()

    def __init__(self, pipeline: Pipeline, project_id: str, options: ProjectOptions, *,
                 resume_analysis: bool = False, explicit_retry: bool = False) -> None:
        super().__init__()
        self.pipeline = pipeline
        self.project_id = project_id
        self.options = options
        self.resume_analysis = resume_analysis
        self.explicit_retry = explicit_retry

    @Slot()
    def run(self) -> None:
        try:
            clips = self.pipeline.run(self.project_id, self.options, self.progress.emit,
                                      resume_analysis=self.resume_analysis, explicit_retry=self.explicit_retry,
                                      warning=self.warning.emit,
                                      download_progress=self.download_progress.emit, download_log=self.download_log.emit,
                                      speech_progress=self.speech_progress.emit, speech_log=self.speech_log.emit)
            self.completed.emit(self.project_id, len(clips))
        except PausedError:
            self.paused.emit(self.project_id, "Download paused (partial files preserved)")
        except CancelledError:
            self.failed.emit(self.project_id, "Processing cancelled")
        except Exception as error:
            self.failed.emit(self.project_id, _format_exception_message(error))
        finally:
            self.finished.emit()

    @Slot()
    def pause(self) -> None:
        self.pipeline.manager.pause()

    @Slot()
    def cancel(self) -> None:
        self.pipeline.manager.cancel()

