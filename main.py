import atexit
import os
from pathlib import Path

from autoclip_studio.gui import run


if __name__ == "__main__":
    pid_file = Path(__file__).resolve().parent / ".autoclip.pid"
    pid_file.write_text(str(os.getpid()), encoding="ascii")
    atexit.register(lambda: pid_file.unlink(missing_ok=True))
    raise SystemExit(run())
