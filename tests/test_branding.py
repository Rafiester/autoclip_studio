from __future__ import annotations

import os
import struct
from pathlib import Path

from PySide6.QtGui import QIcon, QImage
from PySide6.QtWidgets import QApplication, QLabel

from autoclip_studio.paths import asset_path


def _app() -> QApplication:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QApplication.instance() or QApplication([])


def test_brand_assets_resolve_from_any_cwd(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    for name in ("AutoClipTrans.png", "AutoClipJpg.jpg", "AutoClipSvg.svg", "autoclip.ico", "symbol.png"):
        path = asset_path(name)
        assert path.is_file(), f"Missing asset: {name}"
        assert os.path.isabs(path)


def test_multires_ico_contains_expected_sizes_and_transparent_corners() -> None:
    ico_path = asset_path("autoclip.ico")
    data = ico_path.read_bytes()
    reserved, kind, count = struct.unpack_from("<HHH", data, 0)
    assert reserved == 0
    assert kind == 1
    assert count == 7
    sizes = []
    for index in range(count):
        width, height = struct.unpack_from("<BB", data, 6 + index * 16)
        sizes.append(256 if width == 0 else width)
        assert width == height
    assert sizes == [16, 24, 32, 48, 64, 128, 256]

    symbol = QImage(str(asset_path("symbol.png")))
    assert not symbol.isNull()
    assert symbol.hasAlphaChannel()
    assert symbol.pixelColor(0, 0).alpha() == 0


def test_main_window_displays_wordmark_and_icon(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AUTOCLIP_DATA_DIR", str(tmp_path / "data"))
    app = _app()
    app.setWindowIcon(QIcon(str(asset_path("autoclip.ico"))))
    from autoclip_studio.gui import MainWindow

    window = MainWindow()
    try:
        assert not window.windowIcon().isNull()
        brand = window.findChild(QLabel, "brand")
        assert brand is not None
        pixmap = brand.pixmap()
        assert pixmap is not None and not pixmap.isNull()
    finally:
        window.close()
