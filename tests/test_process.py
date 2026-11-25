import os
import subprocess
import sys
import threading

import pytest

from autoclip_studio.process import CancelledError, ProcessManager


@pytest.mark.skipif(os.name != "nt", reason="Tests Windows Job Objects")
def test_cancel_terminates_child_and_grandchild():
    import win32api
    import win32con
    import win32event

    runner = ProcessManager()
    ready = threading.Event()
    child_ids = []
    errors = []
    script = (
        "import subprocess, sys, time; "
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(90)']); "
        "print(child.pid, flush=True); time.sleep(90)"
    )

    def run():
        try:
            runner.run([sys.executable, "-c", script],
                       on_output=lambda line: (child_ids.append(int(line)), ready.set()))
        except Exception as error:
            errors.append(error)

    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert ready.wait(10), errors
        child = win32api.OpenProcess(win32con.SYNCHRONIZE, False, child_ids[0])
        try:
            runner.cancel()
            assert win32event.WaitForSingleObject(child, 5000) == win32event.WAIT_OBJECT_0
        finally:
            child.Close()
    finally:
        runner.cancel()
        worker.join(10)
    assert not worker.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], CancelledError)
