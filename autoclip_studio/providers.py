from __future__ import annotations

import json
import mimetypes
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests


class ProviderError(RuntimeError):
    pass


@dataclass(slots=True)
class AIProvider:
    kind: str
    api_key: str
    base_url: str
    model: str
    timeout: int = 90

    def complete(self, system: str, prompt: str) -> str:
        if not self.api_key:
            raise ProviderError("No API key is configured for the selected provider.")
        self._validate_url()
        if self.kind == "gemini":
            return self._gemini(system, prompt)
        return self._openai(system, prompt)

    def test(self) -> str:
        result = self.complete("Answer concisely.", "Reply with exactly: connection successful")
        if not result.strip():
            raise ProviderError("The provider returned an empty response.")
        return result.strip()

    def complete_video(self, video_path: Path, prompt: str) -> str:
        """Upload a video to Gemini Files, analyze it, then remove the remote file."""
        if self.kind != "gemini":
            raise ProviderError("Multimodal video analysis is available only with Gemini.")
        if not self.api_key:
            raise ProviderError("No Gemini API key is configured.")
        self._validate_url()
        mime = mimetypes.guess_type(video_path.name)[0] or "video/mp4"
        base = self.base_url.rstrip("/")
        upload_base = base.rsplit("/v1beta", 1)[0] + "/upload/v1beta" if "/v1beta" in base else base + "/upload/v1beta"
        start = requests.post(
            f"{upload_base}/files", params={"key": self.api_key},
            headers={
                "X-Goog-Upload-Protocol": "resumable", "X-Goog-Upload-Command": "start",
                "X-Goog-Upload-Header-Content-Length": str(video_path.stat().st_size),
                "X-Goog-Upload-Header-Content-Type": mime, "Content-Type": "application/json",
            },
            json={"file": {"display_name": video_path.name}}, timeout=60,
        )
        self._raise(start)
        upload_url = start.headers.get("X-Goog-Upload-URL")
        if not upload_url:
            raise ProviderError("Gemini did not return a resumable upload URL.")
        with video_path.open("rb") as stream:
            uploaded = requests.post(
                upload_url, data=stream,
                headers={"Content-Length": str(video_path.stat().st_size), "X-Goog-Upload-Offset": "0",
                         "X-Goog-Upload-Command": "upload, finalize"}, timeout=max(self.timeout, 600),
            )
        self._raise(uploaded)
        file_info = uploaded.json().get("file", uploaded.json())
        file_name = file_info.get("name", "")
        try:
            deadline = time.monotonic() + 600
            while file_info.get("state") == "PROCESSING" and time.monotonic() < deadline:
                time.sleep(2)
                status = requests.get(f"{base}/{file_name}", params={"key": self.api_key}, timeout=30)
                self._raise(status)
                file_info = status.json()
            if file_info.get("state") not in {None, "ACTIVE"}:
                raise ProviderError(f"Gemini video processing ended in state {file_info.get('state')}.")
            return self._gemini_parts([
                {"fileData": {"mimeType": file_info.get("mimeType", mime), "fileUri": file_info["uri"]}},
                {"text": prompt},
            ], "You are an expert short-form video editor. Return only the requested JSON.")
        finally:
            if file_name:
                try:
                    requests.delete(f"{base}/{file_name}", params={"key": self.api_key}, timeout=30)
                except requests.RequestException:
                    pass

    def list_models(self) -> list[str]:
        if not self.api_key:
            raise ProviderError("Enter an API key first.")
        self._validate_url()
        if self.kind == "gemini":
            url = f"{self.base_url.rstrip('/')}/models"
            response = requests.get(url, params={"key": self.api_key}, timeout=30)
            self._raise(response)
            return sorted(
                item["name"].removeprefix("models/") for item in response.json().get("models", [])
                if "generateContent" in item.get("supportedGenerationMethods", [])
            )
        response = requests.get(
            f"{self.base_url.rstrip('/')}/models",
            headers={"Authorization": f"Bearer {self.api_key}"}, timeout=30,
        )
        self._raise(response)
        return sorted(str(item["id"]) for item in response.json().get("data", []))

    def _gemini(self, system: str, prompt: str) -> str:
        return self._gemini_parts([{"text": prompt}], system)

    def _gemini_parts(self, parts: list[dict[str, Any]], system: str) -> str:
        url = f"{self.base_url.rstrip('/')}/models/{self.model}:generateContent"
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.25},
        }
        response = requests.post(url, params={"key": self.api_key}, json=payload, timeout=self.timeout)
        self._raise(response)
        try:
            return response.json()["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError) as error:
            raise ProviderError("Gemini returned an unexpected response.") from error

    def _openai(self, system: str, prompt: str) -> str:
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            "temperature": 0.25,
            "response_format": {"type": "json_object"},
        }
        response = requests.post(
            f"{self.base_url.rstrip('/')}/chat/completions", json=payload,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            timeout=self.timeout,
        )
        if response.status_code == 400 and "response_format" in response.text:
            payload.pop("response_format")
            response = requests.post(
                f"{self.base_url.rstrip('/')}/chat/completions", json=payload,
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                timeout=self.timeout,
            )
        self._raise(response)
        try:
            return response.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise ProviderError("The API returned an unexpected response.") from error

    @staticmethod
    def _raise(response: requests.Response) -> None:
        try:
            response.raise_for_status()
        except requests.HTTPError as error:
            try:
                detail: Any = response.json()
                detail = detail.get("error", detail)
                if isinstance(detail, dict):
                    detail = detail.get("message", json.dumps(detail))
            except ValueError:
                detail = response.text[:400]
            raise ProviderError(f"Provider request failed ({response.status_code}): {detail}") from error

    def _validate_url(self) -> None:
        parsed = urlparse(self.base_url)
        local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if not parsed.hostname or (parsed.scheme != "https" and not (local and parsed.scheme == "http")):
            raise ProviderError("Base URL must use HTTPS (HTTP is allowed only for localhost).")
