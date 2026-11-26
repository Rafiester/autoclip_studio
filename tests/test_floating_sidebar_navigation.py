"""Regression tests for the floating collapsible sidebar, nested Settings navigation, and Appearance page."""
from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtWidgets import QApplication

from autoclip_studio.config import SettingsStore
from autoclip_studio.database import Database
from autoclip_studio.gui import MainWindow
from autoclip_studio.ui.sidebar import PRIMARY_NAV_ROUTES, SETTINGS_CHILD_ROUTES, ThemePreviewCard


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_floating_sidebar_hierarchy_collapse_routing_and_persistence(tmp_path: Path, monkeypatch) -> None:
    app = _app()
    db = Database(tmp_path / "app.db")
    settings_path = tmp_path / "settings.json"
    settings1 = SettingsStore(settings_path)
    monkeypatch.setattr(settings1, "api_key", lambda *_a: "")
    monkeypatch.setattr(settings1, "set_api_key", lambda *_a: None)
    monkeypatch.setattr("autoclip_studio.gui.Database", lambda: db)
    monkeypatch.setattr("autoclip_studio.gui.SettingsStore", lambda: settings1)

    win1 = MainWindow()
    try:
        win1.resize(1280, 860)
        win1.show()
        app.processEvents()

        # 1. Exact primary & Settings child hierarchy, clean footer, zero sidebar theme bar or tagline
        assert [r.label for r in PRIMARY_NAV_ROUTES] == [
            "Dashboard", "New Project", "Processing Queue", "Generated Clips", "Caption Templates",
        ]
        assert [r.label for r in SETTINGS_CHILD_ROUTES] == [
            "General", "Appearance", "AI Providers", "Transcription", "Performance", "Dependencies",
        ]
        assert "Version" in win1.sidebar.version_label.text()
        assert "Local-first" not in win1.sidebar.version_label.text()
        # No theme switcher inside sidebar; all 3 ThemePreviewCards live in Settings > Appearance
        assert win1.sidebar.findChildren(ThemePreviewCard) == []
        assert set(win1.appearance_buttons.keys()) == {"light", "dark", "system"}
        for card in win1.appearance_buttons.values():
            assert isinstance(card, ThemePreviewCard)

        # 2. Expanded -> Collapsed expands main content width and preserves active page
        win1.show_page(2)
        app.processEvents()
        expanded_content_w = win1.stack.width()
        assert win1.sidebar.width() == win1.sidebar.EXPANDED_WIDTH
        win1.sidebar.toggle_collapsed()
        app.processEvents()
        assert win1.sidebar.collapsed is True
        assert win1.sidebar.width() == win1.sidebar.COLLAPSED_WIDTH
        assert win1.stack.width() > expanded_content_w
        assert win1.stack.currentIndex() == 2
        assert win1.sidebar.primary_buttons["queue"].toolTip() == "Processing Queue"
        assert win1.sidebar_compact_check.isChecked() is True

        # Expand back & navigate Settings tree
        win1.sidebar.toggle_collapsed()
        app.processEvents()
        assert win1.sidebar.collapsed is False
        assert win1.sidebar_compact_check.isChecked() is False

        # 3. Expandable Settings & parent-child active association
        win1.show_route("Settings")
        app.processEvents()
        assert win1.stack.currentIndex() == 9  # Settings > General
        assert win1.sidebar.settings_expanded is True
        assert win1.sidebar.settings_tree.isVisible() is True
        assert win1.sidebar.settings_button.is_visually_active() is True
        assert win1.sidebar.settings_child_buttons["settings.general"].isChecked() is True
        assert win1.sidebar.settings_child_buttons["settings.appearance"].isChecked() is False

        # 4. Legacy route redirects resolve to exact relocated pages
        win1.show_route("AI Provider Settings")
        assert win1.stack.currentIndex() == 5
        win1.show_route("Speech-to-Text")
        assert win1.stack.currentIndex() == 6
        win1.show_route("Performance")
        assert win1.stack.currentIndex() == 7
        win1.show_route("Dependencies")
        assert win1.stack.currentIndex() == 8

        # 5. Settings > Appearance switches theme immediately and persists
        win1.show_route("settings.appearance")
        app.processEvents()
        assert win1.stack.currentIndex() == 10
        win1.appearance_buttons["light"].click()
        app.processEvents()
        assert win1._theme.resolved == "light"
        assert win1.appearance_buttons["light"].isChecked() is True
        assert win1.appearance_buttons["dark"].isChecked() is False

        # 6. Small window with expanded Settings does not overflow horizontally or hide footer
        win1.resize(780, 580)
        app.processEvents()
        assert win1.sidebar.version_label.isVisible()
        assert win1.sidebar.width() == win1.sidebar.EXPANDED_WIDTH
        assert win1.stack.width() >= 480

        # Persist collapsed state & last settings route before close
        win1.sidebar.set_collapsed(True)
        app.processEvents()
    finally:
        win1.close()
        app.processEvents()

    # 7. Restart persistence check
    settings2 = SettingsStore(settings_path)
    monkeypatch.setattr(settings2, "api_key", lambda *_a: "")
    monkeypatch.setattr(settings2, "set_api_key", lambda *_a: None)
    monkeypatch.setattr("autoclip_studio.gui.SettingsStore", lambda: settings2)
    assert settings2.ui_state()["sidebar_collapsed"] is True
    assert settings2.ui_state()["last_settings_route"] == "settings.appearance"
    assert settings2.data["appearance"] == "light"

    win2 = MainWindow()
    try:
        assert win2.sidebar.collapsed is True
        assert win2.sidebar.last_settings_route == "settings.appearance"
        assert win2._theme.resolved == "light"
        assert win2.appearance_buttons["light"].isChecked() is True
    finally:
        win2.close()
        app.processEvents()
