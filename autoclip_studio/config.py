from __future__ import annotations

import copy
import json
import os
import shutil
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import keyring

from .captions import TEMPLATE_DEFAULT
from .diagnostics import LOG
from .paths import SETTINGS_PATH, ensure_directories
from .performance import PerformanceSettings
from .speech_engine import SpeechSettings


SERVICE_NAME = "AutoClip Studio"
SCHEMA_VERSION = 2
_FORBIDDEN_JSON_KEYS = frozenset({"api_key", "apikey", "token", "access_token", "refresh_token", "password", "secret"})


def credential_store_name() -> str:
    """Return a human-readable name for the active OS credential storage backend."""
    try:
        backend_name = type(keyring.get_keyring()).__name__.lower()
    except Exception:
        backend_name = ""
    if "macos" in backend_name or "osx" in backend_name or "keychain" in backend_name:
        return "macOS Keychain"
    if "win" in backend_name or "vault" in backend_name:
        return "Windows Credential Manager"
    if sys.platform == "darwin":
        return "macOS Keychain"
    if os.name == "nt":
        return "Windows Credential Manager"
    return "System Keyring"


@dataclass(slots=True)
class ProviderSettings:
    provider: str = "gemini"
    base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    model: str = "gemini-2.5-flash"
    fallback_models: list[str] = field(default_factory=list)
    auto_fallback: bool = False
    display_name: str = ""
    cached_models: list[str] = field(default_factory=list)
    models_updated_at: int = 0


def _default_provider_map() -> dict[str, dict[str, Any]]:
    return {
        "gemini": asdict(ProviderSettings(
            provider="gemini",
            base_url="https://generativelanguage.googleapis.com/v1beta",
            model="gemini-2.5-flash",
            display_name="Google Gemini",
        )),
        "openai": asdict(ProviderSettings(
            provider="openai",
            base_url="https://api.openai.com/v1",
            model="gpt-4o-mini",
            display_name="OpenAI",
        )),
        "custom": asdict(ProviderSettings(
            provider="custom",
            base_url="",
            model="",
            display_name="Custom OpenAI-Compatible",
        )),
    }


DEFAULTS: dict[str, Any] = {
    "schema_version": SCHEMA_VERSION,
    "active_provider": "gemini",
    "providers": _default_provider_map(),
    "cache_enabled": True,
    "caption_template": TEMPLATE_DEFAULT,
    "output_dir": "",
    "whisper_model": "small",
    "whisper_device": "auto",
    "speech": asdict(SpeechSettings()),
    "performance": asdict(PerformanceSettings()),
    "hardware_encoding": True,
    "appearance": "dark",
    "project": {
        "clip_count": 3,
        "min_duration": 25,
        "max_duration": 60,
        "aspect_ratio": "9:16",
        "resolution": "1080x1920",
        "reframe_mode": "auto",
        "reframe_style": "natural",
        "captions": True,
        "cache_enabled": True,
        "highlight_mode": "hybrid",
        "ai_weight_pct": 60,
        "multimodal": False,
        "use_youtube_subtitles": False,
    },
    "asd": {
        "prefer_yunet": True,
        "min_hold_seconds": 1.6,
        "split_screen_auto": True,
    },
    "downloader": {
        "preferred_max_height": 1080,
        "prefer_js_runtime": True,
        "max_retries": 3,
    },
    "dependencies": {
        "cloud_only": False,
        "auto_check_updates": False,
    },
    "ui": {
        "window_width": 1280,
        "window_height": 820,
        "window_maximized": False,
        "last_page": 0,
        "player_volume": 80,
        "player_muted": False,
        "sidebar_collapsed": False,
        "settings_expanded": False,
        "last_settings_route": "settings.general",
    },
}


def _strip_secrets(obj: Any) -> Any:
    """Recursively ensure no plaintext credential fields are ever persisted into settings.json."""
    if isinstance(obj, dict):
        return {
            str(k): _strip_secrets(v)
            for k, v in obj.items()
            if str(k).lower() not in _FORBIDDEN_JSON_KEYS
        }
    if isinstance(obj, list):
        return [_strip_secrets(item) for item in obj]
    return obj


def _merge_dict(defaults: dict[str, Any], saved: Any) -> dict[str, Any]:
    merged = copy.deepcopy(defaults)
    if not isinstance(saved, dict):
        return merged
    for key, value in saved.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _merge_dict(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _clean_str_list(raw: Any) -> list[str]:
    if not isinstance(raw, (list, tuple)):
        return []
    seen: list[str] = []
    for item in raw:
        text = str(item).strip()
        if text and text not in seen:
            seen.append(text)
    return seen


def _migrate_settings(saved: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Migrate any older v0/v1 settings.json cleanly to SCHEMA_VERSION while preserving all valid fields."""
    changed = False
    raw_version = saved.get("schema_version")
    try:
        prev_version = int(raw_version) if raw_version is not None else 1
    except (TypeError, ValueError):
        prev_version = 1

    clean_saved = _strip_secrets(saved)
    if clean_saved != saved:
        changed = True

    merged = copy.deepcopy(DEFAULTS)
    for key, value in clean_saved.items():
        if key == "providers":
            continue
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _merge_dict(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)

    # Merge providers individually so every built-in and custom provider keeps defaults + cached_models + model.
    default_providers = _default_provider_map()
    saved_providers = clean_saved.get("providers") if isinstance(clean_saved.get("providers"), dict) else {}
    for name, prov_defaults in default_providers.items():
        entry = copy.deepcopy(prov_defaults)
        if isinstance(saved_providers.get(name), dict):
            entry.update(copy.deepcopy(saved_providers[name]))
        entry["provider"] = name
        entry["base_url"] = str(entry.get("base_url") or "").strip()
        entry["model"] = str(entry.get("model") or "").strip()
        entry["display_name"] = str(entry.get("display_name") or prov_defaults["display_name"]).strip()
        entry["fallback_models"] = _clean_str_list(entry.get("fallback_models"))
        entry["cached_models"] = _clean_str_list(entry.get("cached_models"))
        entry["auto_fallback"] = bool(entry.get("auto_fallback", False))
        try:
            entry["models_updated_at"] = int(entry.get("models_updated_at") or 0)
        except (TypeError, ValueError):
            entry["models_updated_at"] = 0
        merged["providers"][name] = entry

    # Preserve any extra user-defined provider keys if present.
    for name, raw_entry in saved_providers.items():
        if name not in merged["providers"] and isinstance(raw_entry, dict):
            entry = copy.deepcopy(raw_entry)
            entry["provider"] = str(name)
            entry["model"] = str(entry.get("model") or "").strip()
            entry["base_url"] = str(entry.get("base_url") or "").strip()
            entry["fallback_models"] = _clean_str_list(entry.get("fallback_models"))
            entry["cached_models"] = _clean_str_list(entry.get("cached_models"))
            merged["providers"][name] = entry

    # Validate active_provider
    if merged.get("active_provider") not in merged["providers"]:
        merged["active_provider"] = "gemini"
        changed = True

    # Sync legacy top-level cache_enabled into project defaults if project block didn't exist in v1.
    if "project" not in clean_saved and "cache_enabled" in clean_saved:
        merged["project"]["cache_enabled"] = bool(clean_saved["cache_enabled"])

    if prev_version < SCHEMA_VERSION:
        changed = True
    merged["schema_version"] = SCHEMA_VERSION
    return merged, changed


class SettingsStore:
    """Thread-safe, versioned, atomic single-source-of-truth local settings manager."""

    def __init__(self, path: Path = SETTINGS_PATH) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._debounce_timer: threading.Timer | None = None
        ensure_directories()
        self.data, migrated = self._load()
        if migrated and self.path.exists():
            try:
                self.save()
            except OSError as exc:
                LOG.warning("Could not persist migrated settings: %s", exc)

    def _backup_corrupted(self) -> None:
        if not self.path.is_file():
            return
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = self.path.with_name(f"{self.path.stem}.corrupt-{stamp}.bak")
        try:
            shutil.copy2(self.path, backup)
            LOG.warning("Backed up corrupted settings file to %s", backup)
        except OSError as exc:
            LOG.warning("Could not back up corrupted settings file: %s", exc)

    def _load(self) -> tuple[dict[str, Any], bool]:
        if not self.path.exists():
            return copy.deepcopy(DEFAULTS), False
        try:
            raw_text = self.path.read_text(encoding="utf-8")
            saved = json.loads(raw_text)
            if not isinstance(saved, dict):
                raise ValueError("Settings root is not a JSON object")
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            LOG.warning("Settings file %s is unreadable (%s); restoring safe defaults", self.path, exc)
            self._backup_corrupted()
            return copy.deepcopy(DEFAULTS), True
        return _migrate_settings(saved)

    def save(self) -> None:
        """Atomically persist settings immediately (cancels any pending debounced write)."""
        with self._lock:
            if self._debounce_timer is not None:
                self._debounce_timer.cancel()
                self._debounce_timer = None
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.data["schema_version"] = SCHEMA_VERSION
            payload = _strip_secrets(self.data)
            serialized = json.dumps(payload, indent=2, ensure_ascii=False)
            temp = self.path.with_name(f"{self.path.stem}.{os.getpid()}.{threading.get_ident()}.tmp")
            try:
                with temp.open("w", encoding="utf-8") as handle:
                    handle.write(serialized)
                    handle.flush()
                    try:
                        os.fsync(handle.fileno())
                    except OSError:
                        pass
                os.replace(temp, self.path)
            finally:
                if temp.exists():
                    try:
                        temp.unlink()
                    except OSError:
                        pass

    def save_debounced(self, delay_seconds: float = 0.35) -> None:
        """Schedule an atomic write after a short debounce window for high-frequency UI changes."""
        with self._lock:
            if self._debounce_timer is not None:
                self._debounce_timer.cancel()
            timer = threading.Timer(max(0.05, delay_seconds), self._run_debounced_save)
            timer.daemon = True
            self._debounce_timer = timer
            timer.start()

    def _run_debounced_save(self) -> None:
        try:
            self.save()
        except OSError as exc:
            LOG.warning("Debounced settings save failed: %s", exc)

    def flush(self) -> None:
        """Flush any pending debounced settings write immediately (call on shutdown)."""
        with self._lock:
            has_pending = self._debounce_timer is not None
        if has_pending:
            self.save()

    def reset_to_defaults(self, *, clear_credentials: bool = False) -> None:
        """Explicit user-triggered reset to safe defaults (never called during setup or startup)."""
        with self._lock:
            provider_names = list(self.data.get("providers", {}).keys())
            self.data = copy.deepcopy(DEFAULTS)
            self.save()
        if clear_credentials:
            for name in provider_names:
                self.set_api_key(name, "")

    def provider(self, name: str | None = None) -> dict[str, Any]:
        with self._lock:
            selected = name or self.data.get("active_provider", "gemini")
            raw = self.data.get("providers", {}).get(selected)
            if isinstance(raw, dict):
                return copy.deepcopy(raw)
            return copy.deepcopy(_default_provider_map().get(selected, asdict(ProviderSettings(provider=selected, base_url="", model=""))))

    def set_provider(
        self,
        name: str,
        *,
        base_url: str | None = None,
        model: str | None = None,
        display_name: str | None = None,
        fallback_models: list[str] | tuple[str, ...] | None = None,
        auto_fallback: bool | None = None,
        set_active: bool = False,
        persist: bool = True,
    ) -> dict[str, Any]:
        """Update a provider's configuration without clobbering cached_models or other providers."""
        with self._lock:
            providers = self.data.setdefault("providers", _default_provider_map())
            entry = copy.deepcopy(providers.get(name) or _default_provider_map().get(name) or asdict(ProviderSettings(provider=name, base_url="", model="")))
            entry["provider"] = name
            if base_url is not None:
                entry["base_url"] = base_url.strip()
            if model is not None:
                entry["model"] = model.strip()
            if display_name is not None:
                entry["display_name"] = display_name.strip() or entry.get("display_name", name)
            if fallback_models is not None:
                entry["fallback_models"] = _clean_str_list(list(fallback_models))
            if auto_fallback is not None:
                entry["auto_fallback"] = bool(auto_fallback)
            providers[name] = entry
            if set_active:
                self.data["active_provider"] = name
            result = copy.deepcopy(entry)
        if persist:
            self.save()
        return result

    def set_provider_model(self, name: str, model: str, *, allow_empty: bool = False, persist: bool = True) -> str:
        """Persist the selected model ID for one provider independently."""
        cleaned = model.strip()
        if not cleaned and not allow_empty:
            return self.provider(name).get("model", "")
        self.set_provider(name, model=cleaned, persist=persist)
        return cleaned

    def set_cached_models(self, name: str, models: list[str] | tuple[str, ...], *, persist: bool = True) -> list[str]:
        """Cache discovered model IDs separately from the user's selected model (never clears selected model)."""
        cleaned = _clean_str_list(list(models))
        with self._lock:
            providers = self.data.setdefault("providers", _default_provider_map())
            entry = copy.deepcopy(providers.get(name) or _default_provider_map().get(name) or asdict(ProviderSettings(provider=name, base_url="", model="")))
            entry["cached_models"] = cleaned
            entry["models_updated_at"] = int(time.time())
            providers[name] = entry
        if persist:
            self.save()
        return cleaned

    def resolve_provider_config(
        self,
        name: str | None = None,
        *,
        model_override: str | None = None,
    ) -> dict[str, Any]:
        """Return a complete, immutable snapshot of the resolved provider configuration."""
        selected = (name or self.data.get("active_provider") or "gemini").strip()
        cfg = self.provider(selected)
        resolved_model = (model_override if model_override is not None else "").strip() or str(cfg.get("model") or "").strip()
        return {
            "provider": selected,
            "display_name": str(cfg.get("display_name") or selected),
            "base_url": str(cfg.get("base_url") or "").strip(),
            "model": resolved_model,
            "fallback_models": list(cfg.get("fallback_models") or ()),
            "auto_fallback": bool(cfg.get("auto_fallback", False)),
            "api_key": self.api_key(selected),
        }

    def project_defaults(self) -> dict[str, Any]:
        with self._lock:
            return _merge_dict(DEFAULTS["project"], self.data.get("project"))

    def update_project_defaults(self, updates: dict[str, Any], *, debounced: bool = True) -> None:
        with self._lock:
            current = _merge_dict(DEFAULTS["project"], self.data.get("project"))
            for key, value in updates.items():
                if key in current:
                    current[key] = value
            self.data["project"] = current
            if "cache_enabled" in updates:
                self.data["cache_enabled"] = bool(updates["cache_enabled"])
        if debounced:
            self.save_debounced()
        else:
            self.save()

    def ui_state(self) -> dict[str, Any]:
        with self._lock:
            return _merge_dict(DEFAULTS["ui"], self.data.get("ui"))

    def update_ui_state(self, updates: dict[str, Any], *, debounced: bool = True) -> None:
        with self._lock:
            current = _merge_dict(DEFAULTS["ui"], self.data.get("ui"))
            for key, value in updates.items():
                if key in current:
                    current[key] = value
            self.data["ui"] = current
        if debounced:
            self.save_debounced()
        else:
            self.save()

    def speech(self) -> SpeechSettings:
        with self._lock:
            values = self.data.get("speech", {})
            defaults = asdict(SpeechSettings())
            if isinstance(values, dict):
                defaults.update({key: value for key, value in values.items() if key in defaults})
            return SpeechSettings(**defaults)

    def performance(self) -> PerformanceSettings:
        with self._lock:
            values = self.data.get("performance", {})
            defaults = asdict(PerformanceSettings())
            if isinstance(values, dict):
                defaults.update({key: value for key, value in values.items() if key in defaults})
            return PerformanceSettings(**defaults)

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


# Alias exposing the centralized SettingsManager name while preserving SettingsStore compatibility.
SettingsManager = SettingsStore
