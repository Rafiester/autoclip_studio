import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest


pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Native Windows launcher")


def test_launcher_checks_the_checkout_and_loads_source_without_rebuild(tmp_path):
    source = Path(__file__).resolve().parent.parent
    checkout = tmp_path / "checkout with spaces"
    checkout.mkdir()
    scripts = checkout / "scripts"
    scripts.mkdir()
    (scripts / "setup.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    launcher = checkout / "AutoClipStudio.exe"
    shutil.copy2(source / launcher.name, launcher)
    binary = launcher.read_bytes()
    app_data = checkout / "test-data"
    env = {**os.environ, "AUTOCLIP_DATA_DIR": str(app_data)}
    entry = checkout / "main.py"
    entry.write_text("raise SystemExit(0)\n", encoding="utf-8")
    assert subprocess.run([launcher, "--check"], cwd=tmp_path, env=env, timeout=15).returncode == 1

    subprocess.run([sys.executable, "-m", "venv", "--without-pip", checkout / ".venv"], check=True,
                   creationflags=subprocess.CREATE_NO_WINDOW, timeout=30)
    assert subprocess.run([launcher, "--check"], cwd=tmp_path, env=env, timeout=15).returncode == 0
    for marker in ("original", "edited source"):
        entry.write_text(
            "import os\nfrom pathlib import Path\n"
            f"Path(os.environ['AUTOCLIP_DATA_DIR'], 'latest.txt').write_text({marker!r}, encoding='utf-8')\n",
            encoding="utf-8",
        )
        assert subprocess.run([launcher], cwd=tmp_path, env=env, timeout=20).returncode == 0
        assert (app_data / "latest.txt").read_text(encoding="utf-8") == marker
        assert launcher.read_bytes() == binary

    import win32api
    import win32con
    import win32event

    entry.write_text(
        "import os, subprocess, sys, time\nfrom pathlib import Path\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], "
        "creationflags=subprocess.CREATE_NO_WINDOW)\n"
        "Path(os.environ['AUTOCLIP_DATA_DIR'], 'child.txt').write_text(str(child.pid))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    process = subprocess.Popen([launcher], cwd=tmp_path, env=env)
    child_file = app_data / "child.txt"
    handle = None
    try:
        deadline = time.monotonic() + 20
        child_pid = ""
        while not child_pid and time.monotonic() < deadline and process.poll() is None:
            if child_file.is_file():
                child_pid = child_file.read_text().strip()
            if not child_pid:
                time.sleep(0.05)
        assert child_pid
        handle = win32api.OpenProcess(win32con.SYNCHRONIZE, False, int(child_pid))
        process.kill()
        process.wait(timeout=10)
        assert win32event.WaitForSingleObject(handle, 5000) == win32event.WAIT_OBJECT_0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        if handle:
            handle.Close()
