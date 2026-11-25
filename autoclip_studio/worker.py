from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QObject, QThread, Signal, Slot

from .diagnostics import log_error, redact
from .models import ProjectOptions
from .pipeline import Pipeline
from .process import CancelledError
from .providers import ProviderError
from .youtube_metadata import YouTubeMetadataError


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
        except (ProviderError, YouTubeMetadataError) as error:
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
    failed = Signal(str, str)
    finished = Signal()

    def __init__(self, pipeline: Pipeline, project_id: str, options: ProjectOptions, *,
                 resume_analysis: bool = False) -> None:
        super().__init__()
        self.pipeline = pipeline
        self.project_id = project_id
        self.options = options
        self.resume_analysis = resume_analysis

    @Slot()
    def run(self) -> None:
        try:
            clips = self.pipeline.run(self.project_id, self.options, self.progress.emit,
                                      resume_analysis=self.resume_analysis, warning=self.warning.emit,
                                      download_progress=self.download_progress.emit, download_log=self.download_log.emit,
                                      speech_progress=self.speech_progress.emit, speech_log=self.speech_log.emit)
            self.completed.emit(self.project_id, len(clips))
        except CancelledError:
            self.failed.emit(self.project_id, "Processing cancelled")
        except Exception as error:
            self.failed.emit(self.project_id, redact(str(error)))
        finally:
            self.finished.emit()

    @Slot()
    def cancel(self) -> None:
        self.pipeline.manager.cancel()

