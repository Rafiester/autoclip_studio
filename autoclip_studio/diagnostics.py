from __future__ import annotations

import logging
import traceback
from logging.handlers import RotatingFileHandler

from .paths import LOG_DIR

LOG = logging.getLogger("autoclip_studio")


def configure_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(LOG_DIR / "autoclip.log", maxBytes=2_000_000,
                                  backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOG.addHandler(handler)
    LOG.setLevel(logging.INFO)


def log_error(operation: str, error: BaseException) -> None:
    # Provider exception messages can contain authentication details.
    frames = "".join(traceback.format_tb(error.__traceback__))
    LOG.error("%s (%s)\n%s", operation, type(error).__name__, frames.rstrip())
