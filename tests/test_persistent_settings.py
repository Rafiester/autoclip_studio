from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from autoclip_studio.config import SCHEMA_VERSION, SettingsManager, SettingsStore
from autoclip_studio.database import Database
from autoclip_studio.providers import AIProvider, ProviderError


def _app() -> QApplication:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QApplication.instance() or QApplication([])


def test_schema_v1_migration_preserves_preferences_and_strips_secrets(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    v1_payload = {
        "active_provider": "custom",
        "providers": {
            "gemini": {"provider": "gemini", "base_url": "https://generativelanguage.googleapis.com/v1beta", "model": "gemini-2.5-pro"},
            "openai": {"provider": "openai", "base_url": "https://api.openai.com/v1", "model": "gpt-4.1-mini"},
            "custom": {
                "provider": "custom",
                "base_url": "https://ai.example.test/v1",
                "model": "deepseek-v4-fast",
                "api_key": "sk-MUST-BE-STRIPPED-FROM-JSON",
                "token": "secret-token-value",
            },
        },
        "cache_enabled": False,
        "caption_template": "Pop Bounce",
        "appearance": "light",
        "hardware_encoding": True,
    }
    path.write_text(json.dumps(v1_payload), encoding="utf-8")

    store = SettingsStore(path)
    assert store.data["schema_version"] == SCHEMA_VERSION
    assert store.data["active_provider"] == "custom"
    assert store.provider("custom")["model"] == "deepseek-v4-fast"
    assert store.provider("gemini")["model"] == "gemini-2.5-pro"
    assert store.provider("openai")["model"] == "gpt-4.1-mini"
    assert store.data["caption_template"] == "Pop Bounce"
    assert store.data["appearance"] == "light"
    assert store.project_defaults()["cache_enabled"] is False
    # No plaintext secrets written back to disk
    on_disk = path.read_text(encoding="utf-8")
    assert "sk-MUST-BE-STRIPPED-FROM-JSON" not in on_disk
    assert "secret-token-value" not in on_disk
    assert "api_key" not in on_disk


def test_corrupted_settings_file_recovers_and_creates_backup(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text("{corrupt json!!!", encoding="utf-8")

    store = SettingsStore(path)
    assert store.data["schema_version"] == SCHEMA_VERSION
    assert store.data["active_provider"] == "gemini"
    backups = list(tmp_path.glob("settings.corrupt-*.bak"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "{corrupt json!!!"


def test_thread_safe_concurrent_saves_and_debounced_flush(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    store = SettingsManager(path)
    errors: list[Exception] = []

    def worker(idx: int) -> None:
        try:
            store.set_provider_model("custom", f"model-{idx}", persist=False)
            store.save_debounced(0.05)
            store.save()
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    store.flush()
    assert not errors
    reloaded = SettingsStore(path)
    assert reloaded.provider("custom")["model"].startswith("model-")


def test_ai_provider_refuses_empty_model_before_http_request(monkeypatch) -> None:
    called = False

    def fail_post(*_a, **_kw):
        nonlocal called
        called = True
        raise AssertionError("HTTP request should never be sent with empty model ID")

    monkeypatch.setattr("autoclip_studio.providers.requests.post", fail_post)
    provider = AIProvider("custom", "test-key", "https://ai.example.test/v1", "")
    with pytest.raises(ProviderError, match="No model ID is configured"):
        provider.complete("sys", "hello")
    assert not called


def test_custom_provider_survives_multiple_restarts_and_single_save_load(tmp_path: Path, monkeypatch) -> None:
    app = _app()
    from autoclip_studio.gui import MainWindow

    settings_path = tmp_path / "settings.json"
    db_path = tmp_path / "app.db"
    vault: dict[str, str] = {}

    monkeypatch.setattr(SettingsStore, "api_key", lambda _self, p: vault.get(p, ""))
    monkeypatch.setattr(SettingsStore, "set_api_key", lambda _self, p, v: vault.__setitem__(p, v) if v else vault.pop(p, None))
    monkeypatch.setattr("autoclip_studio.gui.SettingsStore", lambda: SettingsStore(settings_path))
    monkeypatch.setattr("autoclip_studio.gui.Database", lambda: Database(db_path))

    # Session 1: Save custom provider BEFORE loading models, then Load Models once!
    win1 = MainWindow()
    try:
        win1.show_page(5)
        win1.settings_provider.setCurrentText("custom")
        win1.provider_name_input.setText("AppVerse AI")
        win1.api_key_input.setText("sk-custom-live-secret-999999999999")
        win1.base_url_input.setText("https://ai.example.test/v1")
        win1.save_provider()

        # Simulate Load Models completing once
        monkeypatch.setattr(AIProvider, "list_models", lambda _self: ["deepseek-v4-fast", "qwen-3-max"])
        win1.load_models()
        for t in list(win1.utility_threads):
            t.wait(3000)
        app.processEvents()

        # Model auto-selected, persisted, AND synced to New Project without a second Save!
        assert win1.model_input.currentText() == "deepseek-v4-fast"
        assert win1.project_model.text() == "deepseek-v4-fast"

        # User selects second model and updates New Project preferences
        win1.model_input.setCurrentIndex(1)
        win1._on_settings_model_committed()
        assert win1.project_model.text() == "qwen-3-max"

        win1.reframe_combo.setCurrentIndex(win1.reframe_combo.findData("split_screen"))
        win1.clip_count.setValue(5)
        win1.hybrid_weight_spin.setValue(75)
        win1.resize(1340, 860)
    finally:
        win1.close()
        app.processEvents()
        win1.deleteLater()

    # Verify API key is NOT in plaintext settings.json
    raw_json = settings_path.read_text(encoding="utf-8")
    assert "sk-custom-live-secret-999999999999" not in raw_json

    # Session 2 & Session 3: Relaunch twice and verify EVERYTHING restored immediately without Load Models!
    for _relaunch in range(2):
        win = MainWindow()
        try:
            assert win.project_provider.currentText() == "custom"
            assert win.project_model.text() == "qwen-3-max"
            assert win.settings_provider.currentText() == "custom"
            assert win.provider_name_input.text() == "AppVerse AI"
            assert win.base_url_input.text() == "https://ai.example.test/v1"
            assert win.api_key_input.text() == "sk-custom-live-secret-999999999999"
            assert win.model_input.currentText() == "qwen-3-max"
            # Cached models preserved in dropdown across restarts
            dropdown_items = [win.model_input.itemText(i) for i in range(win.model_input.count())]
            assert "deepseek-v4-fast" in dropdown_items and "qwen-3-max" in dropdown_items
            # New Project preferences & window size preserved
            assert win.reframe_combo.currentData() == "split_screen"
            assert win.clip_count.value() == 5
            assert win.hybrid_weight_spin.value() == 75
            assert win.width() == 1340 and win.height() == 860
        finally:
            win.close()
            app.processEvents()
            win.deleteLater()


def test_provider_switching_manual_model_and_failed_discovery_preserve_selection(tmp_path: Path, monkeypatch) -> None:
    app = _app()
    from autoclip_studio.gui import MainWindow

    settings_path = tmp_path / "settings.json"
    db_path = tmp_path / "app.db"
    vault: dict[str, str] = {"gemini": "gem-key", "openai": "oai-key", "custom": "cust-key"}

    monkeypatch.setattr(SettingsStore, "api_key", lambda _self, p: vault.get(p, ""))
    monkeypatch.setattr(SettingsStore, "set_api_key", lambda _self, p, v: vault.__setitem__(p, v))
    monkeypatch.setattr("autoclip_studio.gui.SettingsStore", lambda: SettingsStore(settings_path))
    monkeypatch.setattr("autoclip_studio.gui.Database", lambda: Database(db_path))

    win = MainWindow()
    try:
        # Configure independent models for each provider
        win.settings_provider.setCurrentText("gemini")
        win.model_input.setEditText("gemini-2.5-pro")
        win.save_provider()

        win.settings_provider.setCurrentText("openai")
        win.model_input.setEditText("gpt-4.1")
        win.save_provider()

        win.settings_provider.setCurrentText("custom")
        win.base_url_input.setText("https://custom.example/v1")
        win.model_input.setEditText("my-manual-model-id")
        win.save_provider()

        # Switch across providers and confirm each restores its own model independently
        win.settings_provider.setCurrentText("gemini")
        assert win.model_input.currentText() == "gemini-2.5-pro"
        win.settings_provider.setCurrentText("openai")
        assert win.model_input.currentText() == "gpt-4.1"
        win.settings_provider.setCurrentText("custom")
        assert win.model_input.currentText() == "my-manual-model-id"

        # Refresh model list WITHOUT returning manual model -> manual model MUST still be preserved!
        monkeypatch.setattr(AIProvider, "list_models", lambda _self: ["listed-a", "listed-b"])
        win.load_models()
        for t in list(win.utility_threads):
            t.wait(3000)
        app.processEvents()
        assert win.model_input.currentText() == "my-manual-model-id"
        items = [win.model_input.itemText(i) for i in range(win.model_input.count())]
        assert items == ["my-manual-model-id", "listed-a", "listed-b"]

        # Failed model discovery MUST preserve selected model & cached list
        def boom(_self):
            raise ProviderError("Network failure: timeout", category="network")

        monkeypatch.setattr(AIProvider, "list_models", boom)
        win.load_models()
        for t in list(win.utility_threads):
            t.wait(3000)
        app.processEvents()
        assert win.model_input.currentText() == "my-manual-model-id"
        assert SettingsStore(settings_path).provider("custom")["cached_models"] == ["listed-a", "listed-b"]
    finally:
        win.close()
        app.processEvents()
        win.deleteLater()


def test_stale_async_model_response_ignored_when_provider_switches(tmp_path: Path, monkeypatch) -> None:
    app = _app()
    from autoclip_studio.gui import MainWindow

    settings_path = tmp_path / "settings.json"
    db_path = tmp_path / "app.db"
    vault: dict[str, str] = {"gemini": "gem-key", "custom": "cust-key"}

    monkeypatch.setattr(SettingsStore, "api_key", lambda _self, p: vault.get(p, ""))
    monkeypatch.setattr(SettingsStore, "set_api_key", lambda _self, p, v: vault.__setitem__(p, v))
    monkeypatch.setattr("autoclip_studio.gui.SettingsStore", lambda: SettingsStore(settings_path))
    monkeypatch.setattr("autoclip_studio.gui.Database", lambda: Database(db_path))

    win = MainWindow()
    try:
        win.settings_provider.setCurrentText("custom")
        win.base_url_input.setText("https://custom.example/v1")
        win.model_input.setEditText("custom-model-1")
        win.save_provider()

        gate = threading.Event()

        def slow_list(_self):
            gate.wait(2.0)
            return ["stale-custom-model"]

        monkeypatch.setattr(AIProvider, "list_models", slow_list)
        win.load_models()
        # User switches to gemini while custom model discovery is still in flight
        win.settings_provider.setCurrentText("gemini")
        gate.set()
        for t in list(win.utility_threads):
            t.wait(3000)
        app.processEvents()

        # Gemini's selected model MUST NOT be overwritten by custom's delayed response!
        assert win.settings_provider.currentText() == "gemini"
        assert win.model_input.currentText() == "gemini-2.5-flash"
        # Custom's model list was safely cached under custom without clobbering custom-model-1
        custom_saved = SettingsStore(settings_path).provider("custom")
        assert custom_saved["model"] == "custom-model-1"
        assert custom_saved["cached_models"] == ["stale-custom-model"]
    finally:
        win.close()
        app.processEvents()
        win.deleteLater()
