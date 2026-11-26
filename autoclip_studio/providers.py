from __future__ import annotations

import json
import mimetypes
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from io import BufferedReader
from pathlib import Path
from threading import Event
from typing import Any, Callable
from urllib.parse import urlparse

import requests

from .diagnostics import LOG, redact, redact_response
from .process import CancelledError


MAX_ATTEMPTS = 5
OVERLOAD_CODES = {502, 503, 504}
FALLBACK_ERRORS = {"quota", "rate_limit", "overload", "model"}
_QUOTA_ERROR = re.compile(
    r'(?i)\binsufficient_quota\b|\b(?:billing_hard_limit|(?:project|organization)_(?:spend|usage)_limit)_exceeded\b'
    r'|(?:limit|quota.?value|quota.?limit|allocated quota)\s*["\x27]?\s*(?:[:=]|\bis\b)\s*["\x27]?0(?:\.0+)?(?!\d|\.\d)'
    r'|\b(?:zero (?:allocated )?quota|no (?:allocated )?quota|quota exhausted|quota has been exhausted|'
    r'spend (?:cap|limit) exceeded|(?:credits|funds) (?:exhausted|depleted))\b'
)
_CREDENTIAL_ERROR = re.compile(
    r"(?i)\b(?:api_key_invalid|invalid_api_key|unauthenticated)\b"
    r"|(?:api[- ]?key|credentials?).{0,32}\b(?:not valid|invalid|expired|revoked|missing)\b"
    r"|\binvalid (?:api[- ]?key|credentials?)\b"
)


class ProviderError(RuntimeError):
    def __init__(self, message: str, *, category: str = "response", status: int | None = None) -> None:
        super().__init__(message)
        self.category = category
        self.status = status


def _retry_after(value: str) -> float | None:
    if value.isascii() and value.isdigit():
        try:
            return float(int(value))
        except (OverflowError, ValueError):
            return None
    try:
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        return max(0, (date - datetime.now(timezone.utc)).total_seconds())
    except (OverflowError, TypeError, ValueError):
        return None


class _UploadStream(BufferedReader):
    def __init__(self, path: Path, cancel_event: Event | None) -> None:
        super().__init__(path.open("rb", buffering=0))
        self.cancel_event = cancel_event

    def read(self, size: int = -1) -> bytes:
        if self.cancel_event and self.cancel_event.is_set():
            raise CancelledError("Processing cancelled")
        return super().read(size)


@dataclass(slots=True)
class AIProvider:
    kind: str
    api_key: str = field(repr=False)
    base_url: str
    model: str
    timeout: int = 90
    fallback_models: tuple[str, ...] = ()
    auto_fallback: bool = False
    cancel_event: Event | None = field(default=None, repr=False)
    notify: Callable[[str], None] | None = field(default=None, repr=False)
    _preferred_model: str | None = field(default=None, init=False, repr=False)

    def complete(self, system: str, prompt: str) -> str:
        if not self.api_key:
            raise ProviderError("No API key is configured for the selected provider.", category="credentials")
        self._validate_url()
        if self.kind == "gemini":
            return self._with_models(lambda model: self._gemini_parts([{"text": prompt}], system, model))
        return self._with_models(lambda model: self._openai(system, prompt, model))

    def _with_models(self, send: Callable[[str], str]) -> str:
        models = [self.model]
        if self.auto_fallback:
            models = [self._preferred_model or self.model, self.model, *self.fallback_models]
        if self.kind == "gemini":
            models = [model.strip().removeprefix("models/") for model in models]
        models = list(dict.fromkeys(model.strip() for model in models if model and model.strip()))
        if not models:
            raise ProviderError(
                f"No model ID is configured for the '{self.kind}' provider. "
                "Open AI Provider Settings, select or enter a model ID, and click Save settings.",
                category="model",
            )
        failures = []
        for index, model in enumerate(models):
            try:
                result = send(model)
                self._preferred_model = model
                return result
            except ProviderError as error:
                failures.append(str(error))
                if error.category not in FALLBACK_ERRORS or index + 1 == len(models):
                    if len(failures) == 1:
                        raise
                    details = "\n\n".join([failures[-1], *failures[:-1]])
                    raise ProviderError(details, category=error.category, status=error.status) from None
                self._warn(f"{error}\nTrying fallback model {models[index + 1]}.")
        raise AssertionError("Provider requires at least one model")

    def _warn(self, message: str) -> None:
        safe = redact(message, self.api_key)
        LOG.warning("%s", safe)
        if self.notify:
            self.notify(safe)

    def _request(self, send: Callable[[], requests.Response], *, model: str | None = None,
                 cancel_event: Event | None = None,
                 adapt: Callable[[requests.Response], bool] | None = None,
                 secrets: tuple[str, ...] = ()) -> requests.Response:
        cancel_event = cancel_event if cancel_event is not None else self.cancel_event
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if cancel_event and cancel_event.is_set():
                raise CancelledError("Processing cancelled")
            try:
                response = send()
            except requests.Timeout:
                raise ProviderError("Network failure: the provider request timed out. Check the connection and retry.",
                                    category="network") from None
            except requests.RequestException:
                raise ProviderError("Network failure: could not reach the provider. Check the connection and base URL.",
                                    category="network") from None
            if adapt and attempt < MAX_ATTEMPTS and adapt(response):
                continue
            error = self._response_error(response, model, secrets)
            if error is None:
                return response
            if error.category not in {"overload", "rate_limit"} or attempt == MAX_ATTEMPTS:
                raise error
            delay = 2 ** (attempt - 1) + random.uniform(0, 0.5)
            retry_after = _retry_after(response.headers.get("Retry-After", "").strip())
            if retry_after is not None:
                delay = retry_after if error.category == "rate_limit" else max(delay, retry_after)
            self._warn(f"{error}\nRetrying in {delay:g} seconds (attempt {attempt + 1}/{MAX_ATTEMPTS}).")
            if cancel_event is None:
                time.sleep(delay)
            elif cancel_event.wait(delay):
                raise CancelledError("Processing cancelled")
        raise AssertionError("Retry loop exhausted")

    def test(self) -> str:
        result = self.complete("Answer concisely.", "Reply with exactly: connection successful")
        if not result.strip():
            raise ProviderError("The provider returned an empty response.")
        return redact(result.strip(), self.api_key)

    def complete_video(self, video_path: Path, prompt: str, *, cancel_event: Event | None = None) -> str:
        """Upload a video to Gemini Files, analyze it, then remove the remote file."""
        if self.kind != "gemini":
            raise ProviderError("Multimodal video analysis is available only with Gemini.")
        if not self.api_key:
            raise ProviderError("No Gemini API key is configured.", category="credentials")
        self._validate_url()
        cancel_event = cancel_event if cancel_event is not None else self.cancel_event
        if cancel_event and cancel_event.is_set():
            raise CancelledError("Processing cancelled")
        mime = mimetypes.guess_type(video_path.name)[0] or "video/mp4"
        base = self.base_url.rstrip("/")
        upload_base = base.rsplit("/v1beta", 1)[0] + "/upload/v1beta" if "/v1beta" in base else base + "/upload/v1beta"
        start = self._request(lambda: requests.post(
            f"{upload_base}/files", params={"key": self.api_key},
            headers={
                "X-Goog-Upload-Protocol": "resumable", "X-Goog-Upload-Command": "start",
                "X-Goog-Upload-Header-Content-Length": str(video_path.stat().st_size),
                "X-Goog-Upload-Header-Content-Type": mime, "Content-Type": "application/json",
            },
            json={"file": {"display_name": video_path.name}}, timeout=60,
        ), cancel_event=cancel_event)
        upload_url = start.headers.get("X-Goog-Upload-URL")
        if not upload_url:
            raise ProviderError("Gemini did not return a resumable upload URL.")
        if cancel_event and cancel_event.is_set():
            raise CancelledError("Processing cancelled")
        with _UploadStream(video_path, cancel_event) as stream:
            def upload() -> requests.Response:
                stream.seek(0)
                return requests.post(
                    upload_url, data=stream,
                    headers={"Content-Length": str(video_path.stat().st_size), "X-Goog-Upload-Offset": "0",
                             "X-Goog-Upload-Command": "upload, finalize"}, timeout=(10, max(self.timeout, 600)),
                )

            uploaded = self._request(upload, cancel_event=cancel_event, secrets=(upload_url,))
        response_data = self._json_object(uploaded)
        file_info = response_data.get("file", response_data)
        if not isinstance(file_info, dict):
            raise ProviderError("Gemini returned invalid upload metadata.")
        file_name = file_info.get("name", "")
        try:
            deadline = time.monotonic() + 600
            while file_info.get("state") == "PROCESSING" and time.monotonic() < deadline:
                if cancel_event is None:
                    time.sleep(2)
                elif cancel_event.wait(2):
                    raise CancelledError("Processing cancelled")
                status = self._request(lambda: requests.get(
                    f"{base}/{file_name}", params={"key": self.api_key}, timeout=30,
                ), cancel_event=cancel_event, secrets=(upload_url,))
                file_info = self._json_object(status)
            if cancel_event and cancel_event.is_set():
                raise CancelledError("Processing cancelled")
            if file_info.get("state") not in {None, "ACTIVE"}:
                raise ProviderError(redact(f"Gemini video processing ended in state {file_info.get('state')}.", self.api_key))
            if not file_info.get("uri"):
                raise ProviderError("Gemini did not return a URI for the uploaded video.")
            parts = [
                {"fileData": {"mimeType": file_info.get("mimeType", mime), "fileUri": file_info["uri"]}},
                {"text": prompt},
            ]
            return self._with_models(lambda model: self._gemini_parts(
                parts, "You are an expert short-form video editor. Return only the requested JSON.", model,
                cancel_event=cancel_event, secrets=(upload_url, str(file_info["uri"])),
            ))
        finally:
            if file_name:
                try:
                    requests.delete(f"{base}/{file_name}", params={"key": self.api_key}, timeout=30)
                except requests.RequestException:
                    pass

    def list_models(self) -> list[str]:
        if not self.api_key:
            raise ProviderError("Enter an API key first.", category="credentials")
        self._validate_url()
        if self.kind == "gemini":
            url = f"{self.base_url.rstrip('/')}/models"
            response = self._request(lambda: requests.get(url, params={"key": self.api_key}, timeout=30))
            return sorted(
                item["name"].removeprefix("models/") for item in self._json_object(response).get("models", [])
                if "generateContent" in item.get("supportedGenerationMethods", [])
            )
        response = self._request(lambda: requests.get(
            f"{self.base_url.rstrip('/')}/models",
            headers={"Authorization": f"Bearer {self.api_key}"}, timeout=30,
        ))
        return sorted(str(item["id"]) for item in self._json_object(response).get("data", []))

    def _gemini_parts(self, parts: list[dict[str, Any]], system: str, model: str, *,
                      cancel_event: Event | None = None, secrets: tuple[str, ...] = ()) -> str:
        url = f"{self.base_url.rstrip('/')}/models/{model.removeprefix('models/')}:generateContent"
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.25},
        }
        response = self._request(lambda: requests.post(
            url, params={"key": self.api_key}, json=payload, timeout=self.timeout,
        ), model=model, cancel_event=cancel_event, secrets=secrets)
        try:
            result = self._json_object(response)["candidates"][0]["content"]["parts"][0]["text"]
            if isinstance(result, str):
                return result
        except (KeyError, IndexError, TypeError):
            pass
        raise ProviderError("Gemini returned an unexpected response.")

    def _openai(self, system: str, prompt: str, model: str) -> str:
        payload = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            "temperature": 0.25,
            "response_format": {"type": "json_object"},
        }
        def adapt(response: requests.Response) -> bool:
            if "response_format" in payload and response.status_code == 400 and "response_format" in response.text:
                payload.pop("response_format")
                return True
            return False

        response = self._request(lambda: requests.post(
            f"{self.base_url.rstrip('/')}/chat/completions", json=payload,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            timeout=self.timeout,
        ), model=model, adapt=adapt)
        try:
            result = self._json_object(response)["choices"][0]["message"]["content"]
            if isinstance(result, str):
                return result
        except (KeyError, IndexError, TypeError):
            pass
        raise ProviderError("The API returned an unexpected response.")

    @staticmethod
    def _json_object(response: requests.Response) -> dict[str, Any]:
        try:
            value = response.json()
            if isinstance(value, dict):
                return value
        except ValueError:
            pass
        raise ProviderError("The provider returned invalid JSON.")

    def _response_error(self, response: requests.Response, model: str | None,
                        secrets: tuple[str, ...]) -> ProviderError | None:
        status = response.status_code
        if status < 400:
            return None
        body = response.text
        if status in {403, 429} and _QUOTA_ERROR.search(body):
            category, summary = "quota", "Quota exhausted. Check the model's allocation, credits, or billing limits."
        elif status in {401, 403} or _CREDENTIAL_ERROR.search(body):
            category, summary = "credentials", "Invalid credentials or access denied. Check the API key and permissions."
        elif status == 429:
            category, summary = "rate_limit", "Rate limit reached. The provider is throttling requests."
        elif status in OVERLOAD_CODES:
            category, summary = "overload", "Provider server overload or gateway failure."
        elif model and status == 404:
            category, summary = "model", "The selected model or endpoint is unavailable."
        else:
            category, summary = "response", "The provider rejected the request."
        target = f"{self.kind} model {model}" if model else self.kind
        message = f"{summary} ({target}, HTTP {status})"
        if body:
            message += "\n" + redact_response(body, self.api_key, *secrets)
        return ProviderError(redact(message, self.api_key, *secrets), category=category, status=status)

    def _validate_url(self) -> None:
        try:
            parsed = urlparse(self.base_url)
        except ValueError:
            raise ProviderError("The provider base URL is invalid.") from None
        local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if not parsed.hostname or (parsed.scheme != "https" and not (local and parsed.scheme == "http")):
            raise ProviderError("Base URL must use HTTPS (HTTP is allowed only for localhost).")
