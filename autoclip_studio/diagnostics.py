from __future__ import annotations

import json
import logging
import re
import traceback
from logging.handlers import RotatingFileHandler
from urllib.parse import quote, quote_plus

from .paths import LOG_DIR

LOG = logging.getLogger("autoclip_studio")
SUCCESS = 25
logging.addLevelName(SUCCESS, "SUCCESS")

_SECRET_FIELD = (
    r"authorization|proxy[-_]authorization|(?:x[-_](?:goog[-_])?)?api[-_ ]?key|key|"
    r"(?:access|refresh|id|auth|session)[-_]?token|token|(?:client|api)[-_]?secret|"
    r"password|signature|x[-_]goog[-_](?:credential|signature|upload[-_]url)|cookie|set[-_]cookie"
)
_JSON_SECRET = re.compile(_SECRET_FIELD, re.IGNORECASE)
_QUOTED_SECRET = re.compile(rf"""(?i)(["'](?:{_SECRET_FIELD})["']\s*:\s*)(["'])(?:\\.|(?!\2)[^\\])*\2""")
_PLAIN_SECRET = re.compile(
    rf"""(?i)(\b(?:{_SECRET_FIELD})[ \t]*[:=][ \t]*)(?:(?:bearer|basic)\s+)?[^\s,;'"&<>{{}}\[\]]+"""
)
_AUTH_HEADER = re.compile(r"(?im)^([ \t]*(?:proxy-)?authorization[ \t]*:)[^\r\n]*")
_COOKIE_HEADER = re.compile(r"(?im)^([ \t]*(?:set-)?cookie[ \t]*:)[^\r\n]*")
_BEARER = re.compile(r"(?i)\b(?:bearer|basic)\s+[a-zA-Z0-9._~+/=-]{6,}")
_PROVIDER_KEY = re.compile(r"(?<![\w-])(?:AIza[\w-]{35}|sk-[A-Za-z0-9_-]{20,})\b")
_URL_USERINFO = re.compile(r"(?i)(https?://)[^/\s@]+@")


def redact(message: str, *secrets: str) -> str:
    for secret in secrets:
        if secret:
            for form in {secret, quote(secret, safe=""), quote_plus(secret), json.dumps(secret)[1:-1]}:
                if form:
                    message = message.replace(form, "[REDACTED]")
    message = _AUTH_HEADER.sub(r"\1 [REDACTED]", message)
    message = _COOKIE_HEADER.sub(r"\1 [REDACTED]", message)
    message = _QUOTED_SECRET.sub(r"\1\2[REDACTED]\2", message)
    message = _PLAIN_SECRET.sub(r"\1[REDACTED]", message)
    message = _BEARER.sub("[REDACTED]", message)
    message = _PROVIDER_KEY.sub("[REDACTED]", message)
    return _URL_USERINFO.sub(r"\1[REDACTED]@", message)


def redact_response(body: str, *secrets: str) -> str:
    # Gateways may echo request headers as JSON arrays or objects.
    def scrub(value: object) -> bool:
        changed = False
        if isinstance(value, dict):
            for key, entry in value.items():
                if _JSON_SECRET.fullmatch(key):
                    value[key] = "[REDACTED]"
                    changed = True
                elif scrub(entry):
                    changed = True
        elif isinstance(value, list):
            for entry in value:
                if scrub(entry):
                    changed = True
        return changed

    if body.lstrip().startswith(("{", "[")):
        try:
            value = json.loads(body)
            if scrub(value):
                body = json.dumps(value, ensure_ascii=False, indent=2)
        except (ValueError, RecursionError):
            pass
    return redact(body, *secrets)


class _RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def configure_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(LOG_DIR / "autoclip.log", maxBytes=2_000_000,
                                  backupCount=3, encoding="utf-8")
    handler.setFormatter(_RedactingFormatter("%(asctime)s %(levelname)s %(message)s"))
    LOG.addHandler(handler)
    LOG.setLevel(logging.INFO)


def log_error(operation: str, error: BaseException) -> None:
    frames = "".join(traceback.format_tb(error.__traceback__))
    LOG.error("%s (%s)\n%s", operation, type(error).__name__, frames.rstrip())
