from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import threading
from collections import deque
from pathlib import Path
from typing import Callable, Sequence

from .paths import bundled_binary
from .platform_utils import subprocess_creation_flags

if os.name == "nt":
    import pywintypes
    import win32api
    import win32con
    import win32job


class CancelledError(RuntimeError):
    pass


class ProcessManager:
    """Tracks only subprocesses owned by this application."""

    def __init__(self) -> None:
        self.cancel_event = threading.Event()
        self._processes: dict[subprocess.Popen[str], object] = {}
        self._lock = threading.Lock()
        self.default_creationflags = 0

    def reset(self) -> None:
        self.cancel_event.clear()

    def cancel(self) -> None:
        self.cancel_event.set()
        self.terminate_all()

    def check_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise CancelledError("Processing cancelled")

    def run(self, command: Sequence[str | Path], *, cwd: Path | None = None,
            on_output: Callable[[str], None] | None = None,
            env: dict[str, str] | None = None,
            output_tail: int | None = None,
            creationflags_extra: int | None = None) -> subprocess.CompletedProcess[str]:
        extra = self.default_creationflags if creationflags_extra is None else creationflags_extra
        flags = subprocess_creation_flags(extra)
        with self._lock:
            self.check_cancelled()
            job = win32job.CreateJobObject(None, "") if os.name == "nt" else None
            process = None
            try:
                if job is not None:
                    limits = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
                    limits["BasicLimitInformation"]["LimitFlags"] = win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                    win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, limits)
                popen_kwargs: dict[str, object] = {
                    "cwd": cwd,
                    "env": env,
                    "stdin": subprocess.DEVNULL,
                    "stdout": subprocess.PIPE,
                    "stderr": subprocess.STDOUT,
                    "text": True,
                    "encoding": "utf-8",
                    "errors": "replace",
                    "creationflags": flags,
                }
                if os.name != "nt":
                    popen_kwargs["start_new_session"] = True
                process = subprocess.Popen([str(item) for item in command], **popen_kwargs)
                if job is not None and process.poll() is None:
                    handle = win32api.OpenProcess(
                        win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE, False, process.pid
                    )
                    try:
                        win32job.AssignProcessToJobObject(job, handle)
                    except pywintypes.error:
                        if process.poll() is None:
                            raise
                    finally:
                        handle.Close()
                self._processes[process] = job
            except BaseException:
                if job is not None:
                    job.Close()
                if process is not None:
                    process.kill()
                    process.wait()
                    process.stdout.close()
                raise
        lines = [] if output_tail is None else deque(maxlen=output_tail)
        try:
            assert process.stdout is not None
            for line in iter(process.stdout.readline, ""):
                self.check_cancelled()
                stripped = line.rstrip()
                lines.append(stripped)
                if on_output and stripped:
                    on_output(stripped)
            code = process.wait()
            self.check_cancelled()
        finally:
            process.stdout.close()
            with self._lock:
                self._processes.pop(process, None)
                if job is not None:
                    job.Close()
                elif process.poll() is None:
                    self._terminate(process)
            process.wait()
        output = "\n".join(lines)
        result = subprocess.CompletedProcess(command, code, output, "")
        if code:
            raise subprocess.CalledProcessError(code, command, output=output)
        return result

    def terminate_all(self) -> None:
        with self._lock:
            for process, job in self._processes.items():
                if job is not None:
                    win32job.TerminateJobObject(job, 1)
                else:
                    self._terminate(process)

    @staticmethod
    def _terminate(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name != "nt":
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            else:
                process.send_signal(signal.SIGTERM)
            process.wait(timeout=4)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()


def find_media_tool(name: str) -> str:
    bundled = bundled_binary(name)
    if bundled:
        return str(bundled)
    found = shutil.which(name)
    if found:
        return found
    if getattr(sys, "frozen", False):
        raise FileNotFoundError(f"Bundled {name} is missing. Restore the complete application folder.")
    if sys.platform == "darwin":
        raise FileNotFoundError(
            f"{name} was not found. Install ffmpeg-full via Homebrew (`brew install ffmpeg-full`) or run AutoClipStudio.app setup."
        )
    raise FileNotFoundError(
        f"{name} was not found. Run AutoClipStudio.exe --setup to install the project-local FFmpeg tools."
    )

