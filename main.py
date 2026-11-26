import ctypes
import logging
import os
import sys

# On macOS, Qt 6.8's FFmpeg multimedia backend fails to resample 48 kHz AAC audio for
# 44.1 kHz-capped Bluetooth CoreAudio devices, causing silent video playback.
# Selecting Qt's native Apple AVFoundation ("darwin") backend before QApplication initializes
# routes audio through macOS CoreAudio's native hardware sample-rate converter.
if sys.platform == "darwin":
    os.environ.setdefault("QT_MEDIA_BACKEND", "darwin")

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
    try:
        from autoclip_studio.gui import run

        LOG.info("AutoClip Studio starting")
        result = run()
        LOG.info("AutoClip Studio exited with code %s", result)
        return result
    except Exception as error:
        log_error("Application startup or event loop failed", error)
        if os.name == "nt" and not os.environ.get("AUTOCLIP_MANAGED_LAUNCH"):
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
