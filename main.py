import atexit
import ctypes
import logging
import os
import sys
from pathlib import Path

from autoclip_studio.diagnostics import LOG, configure_logging, log_error
from autoclip_studio.paths import LOG_DIR


def main() -> int:
    configure_logging()
    sys.excepthook = lambda _kind, error, _trace: log_error("Unhandled exception", error)
    for stream in ("stdout", "stderr"):
        if getattr(sys, stream) is None:
            setattr(sys, stream, open(os.devnull, "w", encoding="utf-8"))
    if os.name == "nt":
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("AutoClip.Studio")
    if not getattr(sys, "frozen", False) and "--autoclip-studio" in sys.argv:
        pid_file = Path(__file__).resolve().parent / ".autoclip.pid"
        pid_file.write_text(str(os.getpid()), encoding="ascii")
        atexit.register(lambda: pid_file.unlink(missing_ok=True))
    try:
        from autoclip_studio.gui import run

        LOG.info("AutoClip Studio starting")
        result = run()
        LOG.info("AutoClip Studio exited with code %s", result)
        return result
    except Exception as error:
        log_error("Application startup or event loop failed", error)
        if os.name == "nt":
            ctypes.windll.user32.MessageBoxW(
                None, f"AutoClip Studio could not start. See {LOG_DIR / 'autoclip.log'}.",
                "AutoClip Studio", 0x10,
            )
        return 1


if __name__ == "__main__":
    exit_code = main()
    if getattr(sys, "frozen", False):
        # Qt has drained the workers, the database writes are committed, and Job
        # Objects own any remaining media children. Windows DLL detach can hang
        # after ML inference, even after the event loop has exited cleanly.
        logging.shutdown()
        if os.name == "nt":
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.GetCurrentProcess.restype = ctypes.c_void_p
            kernel.TerminateProcess.argtypes = (ctypes.c_void_p, ctypes.c_uint)
            kernel.TerminateProcess(kernel.GetCurrentProcess(), exit_code)
        os._exit(exit_code)
    raise SystemExit(exit_code)
