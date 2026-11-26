from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtWidgets import QApplication

from autoclip_studio.config import SettingsStore
from autoclip_studio.database import Database
from autoclip_studio.ui.theme import (
    DARK_TOKENS, LIGHT_TOKENS, SectionCard, build_theme_stylesheet, load_bundled_fonts, theme_manager,
)


def _app() -> QApplication:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QApplication.instance() or QApplication([])


def test_poppins_bundled_fonts_and_license_present_and_loaded() -> None:
    _app()
    font_dir = Path(__file__).resolve().parent.parent / "autoclip_studio" / "assets" / "fonts" / "poppins"
    for filename in (
        "Poppins-Regular.ttf",
        "Poppins-Medium.ttf",
        "Poppins-SemiBold.ttf",
        "Poppins-Bold.ttf",
        "OFL.txt",
    ):
        assert (font_dir / filename).is_file()
    family = load_bundled_fonts()
    assert family == "Poppins"


def test_theme_tokens_and_stylesheets_cover_light_and_dark() -> None:
    _app()
    assert DARK_TOKENS["bg_app"] == "#101116"
    assert DARK_TOKENS["bg_surface"] == "#1A1C23"
    assert DARK_TOKENS["brand_primary"] == "#E53935"
    assert LIGHT_TOKENS["bg_app"] == "#F1F3F6"
    assert LIGHT_TOKENS["bg_surface"] == "#FFFFFF"
    assert LIGHT_TOKENS["text_primary"] == "#17181C"
    assert LIGHT_TOKENS["brand_primary"] == "#E53935"

    dark_qss = build_theme_stylesheet("dark")
    light_qss = build_theme_stylesheet("light")
    assert "Poppins" in dark_qss and "Poppins" in light_qss
    assert "#101116" in dark_qss and "#F1F3F6" in light_qss
    assert "chevron-up-dark.svg" in dark_qss and "chevron-up-light.svg" in light_qss
    assert "check-white.svg" in dark_qss and "check-white.svg" in light_qss


def test_theme_manager_live_switching_and_persistence(tmp_path: Path, monkeypatch) -> None:
    app = _app()
    from autoclip_studio.gui import MainWindow

    settings_path = tmp_path / "settings.json"
    monkeypatch.setattr("autoclip_studio.gui.Database", lambda: Database(tmp_path / "ui.db"))
    monkeypatch.setattr("autoclip_studio.gui.SettingsStore", lambda: SettingsStore(settings_path))

    win = MainWindow()
    try:
        # Default is Dark Mode
        assert theme_manager().mode == "dark"
        assert theme_manager().resolved == "dark"
        assert win.appearance_buttons["dark"].isChecked()

        # Switch to Light Mode without recreating window
        win.show_page(win.PAGE_AI_PROVIDERS)
        win.set_appearance("light")
        app.processEvents()
        assert theme_manager().mode == "light"
        assert theme_manager().resolved == "light"
        assert win.stack.currentIndex() == win.PAGE_AI_PROVIDERS
        assert win.appearance_buttons["light"].isChecked()

        # Persists across reload
        reloaded = SettingsStore(settings_path)
        assert reloaded.data["appearance"] == "light"

        # System mode resolves cleanly
        win.set_appearance("system")
        app.processEvents()
        assert theme_manager().mode == "system"
        assert theme_manager().resolved in {"dark", "light"}

        # Restore dark for subsequent tests
        win.set_appearance("dark")
    finally:
        win.close()
        app.processEvents()
        win.deleteLater()
