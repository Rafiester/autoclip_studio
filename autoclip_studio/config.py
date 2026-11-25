from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import keyring

from .paths import SETTINGS_PATH, ensure_directories


SERVICE_NAME = "AutoClip Studio"


@dataclass(slots=True)
class ProviderSettings:
    provider: str = "gemini"
    base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    model: str = "gemini-2.5-flash"


DEFAULTS: dict[str, Any] = {
    "active_provider": "gemini",
    "providers": {
        "gemini": asdict(ProviderSettings()),
        "openai": asdict(ProviderSettings("openai", "https://api.openai.com/v1", "gpt-4o-mini")),
        "custom": asdict(ProviderSettings("custom", "", "")),
    },
    "cache_enabled": True,
    "output_dir": "",
    "whisper_model": "small",
    "whisper_device": "auto",
    "hardware_encoding": True,
}


class SettingsStore:
    def __init__(self, path: Path = SETTINGS_PATH) -> None:
        self.path = path
        ensure_directories()
        self.data = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return json.loads(json.dumps(DEFAULTS))
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            saved = {}
        merged = json.loads(json.dumps(DEFAULTS))
        merged.update({k: v for k, v in saved.items() if k != "providers"})
        for name, values in saved.get("providers", {}).items():
            merged["providers"].setdefault(name, {}).update(values)
        return merged

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        os.replace(temp, self.path)

    def provider(self, name: str | None = None) -> dict[str, str]:
        selected = name or self.data["active_provider"]
        return dict(self.data["providers"].get(selected, {}))

    def api_key(self, provider: str) -> str:
        try:
            return keyring.get_password(SERVICE_NAME, provider) or ""
        except keyring.errors.KeyringError:
            return ""

    def set_api_key(self, provider: str, value: str) -> None:
        try:
            if value:
                keyring.set_password(SERVICE_NAME, provider, value)
            else:
                keyring.delete_password(SERVICE_NAME, provider)
        except keyring.errors.PasswordDeleteError:
            pass

