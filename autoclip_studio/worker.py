from __future__ import annotations

from PySide6.QtCore import QObject, Signal, Slot

from .diagnostics import redact
from .models import ProjectOptions
from .pipeline import Pipeline
from .process import CancelledError


class PipelineWorker(QObject):
    progress = Signal(int, str)
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
                                      resume_analysis=self.resume_analysis, warning=self.warning.emit)
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

