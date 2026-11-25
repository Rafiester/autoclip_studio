from __future__ import annotations

import os
import shutil
import signal
import subprocess
import threading
from pathlib import Path
from typing import Callable, Sequence

from .paths import bundled_binary


class CancelledError(RuntimeError):
    pass


class ProcessManager:
    """Tracks only subprocesses owned by this application."""

    def __init__(self) -> None:
        self.cancel_event = threading.Event()
        self._processes: set[subprocess.Popen[str]] = set()
        self._lock = threading.Lock()

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
            env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        self.check_cancelled()
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        process = subprocess.Popen(
            [str(item) for item in command], cwd=cwd, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            creationflags=flags,
        )
        with self._lock:
            self._processes.add(process)
        lines: list[str] = []
        try:
            assert process.stdout is not None
            for line in iter(process.stdout.readline, ""):
                self.check_cancelled()
                stripped = line.rstrip()
                lines.append(stripped)
                if on_output and stripped:
                    on_output(stripped)
            code = process.wait()
        except CancelledError:
            self._terminate(process)
            raise
        finally:
            with self._lock:
                self._processes.discard(process)
        output = "\n".join(lines)
        result = subprocess.CompletedProcess(command, code, output, "")
        if code:
            raise subprocess.CalledProcessError(code, command, output=output)
        return result

    def terminate_all(self) -> None:
        with self._lock:
            processes = list(self._processes)
        for process in processes:
            self._terminate(process)

    @staticmethod
    def _terminate(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW, check=False,
                )
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
    raise FileNotFoundError(
        f"{name} was not found. Run SETUP.bat to install the project-local FFmpeg tools."
    )

