from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtWidgets import QApplication

from autoclip_studio.ui.style import STYLESHEET, TOKENS

LEGACY_PURPLE_HEXES = (
    "#6d5dfc", "#7868ff", "#a99fff", "#c8b7ff", "#7566ff",
    "#8876ff", "#9f90ff", "#7b6cff", "#24233d", "#1a1a29",
)


def _app() -> QApplication:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QApplication.instance() or QApplication([])


def test_brand_tokens_and_no_legacy_purple() -> None:
    assert TOKENS["bg_app"] == "#101116"
    assert TOKENS["brand_primary"] == "#E53935"
    assert TOKENS["brand_accent"] == "#FF5A4F"
    assert TOKENS["text_primary"] == "#F5F5F7"
    assert TOKENS["bg_surface"] == "#191B22"
    assert TOKENS["bg_elevated"] == "#22252E"
    assert TOKENS["border_subtle"] == "#30343E"
    assert TOKENS["text_secondary"] == "#A3A8B5"

    lowered = STYLESHEET.lower()
    for legacy in LEGACY_PURPLE_HEXES:
        assert legacy not in lowered, f"Legacy purple token still present: {legacy}"


def test_all_pages_render_at_standard_and_minimum_sizes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AUTOCLIP_DATA_DIR", str(tmp_path / "data"))
    app = _app()
    from autoclip_studio.gui import MainWindow

    window = MainWindow()
    try:
        for size in ((1280, 820), (760, 560)):
            window.resize(*size)
            for page_index in range(len(window.NAV_ITEMS)):
                window.show_page(page_index)
                app.processEvents()
                assert window.stack.currentIndex() == page_index
                assert window.nav_buttons[page_index].isChecked()
    finally:
        window.close()
