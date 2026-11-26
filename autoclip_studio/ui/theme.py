"""Centralized Design System, Poppins font loader, Light/Dark tokens, and ThemeManager."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Literal

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette
from PySide6.QtWidgets import QApplication, QFrame, QLabel, QVBoxLayout, QWidget

from ..paths import ASSETS_DIR


AppearanceMode = Literal["dark", "light", "system"]
ResolvedTheme = Literal["dark", "light"]

# 4px-based spacing scale (logical pixels)
SPACING: dict[str, int] = {
    "xs": 4,
    "sm": 8,
    "ms": 12,
    "md": 16,
    "lg": 20,
    "xl": 24,
    "2xl": 32,
}

# Border radius tokens (logical pixels)
RADIUS: dict[str, int] = {
    "sm": 6,
    "md": 8,
    "lg": 12,
}

# Typography scale (logical pixels)
TYPOGRAPHY: dict[str, tuple[int, int]] = {
    "page_title": (26, 600),
    "section_heading": (18, 600),
    "card_heading": (15, 600),
    "body": (13, 400),
    "input": (13, 400),
    "button": (13, 500),
    "secondary": (12, 400),
    "helper": (11, 400),
    "metric": (28, 600),
}

DARK_TOKENS: dict[str, str] = {
    "name": "dark",
    # Core surfaces
    "bg_app": "#101116",
    "bg_sidebar": "#16171D",
    "bg_surface": "#1A1C23",
    "bg_elevated": "#22242C",
    "bg_hover": "#2A2D37",
    "bg_pressed": "#15171E",
    "bg_alt_row": "#1E2028",
    "bg_player": "#0B0C10",
    "border_subtle": "#30323A",
    "border_strong": "#434654",
    # Brand red accents
    "brand_primary": "#E53935",
    "brand_primary_hover": "#D32F2F",
    "brand_primary_pressed": "#B71C1C",
    "brand_accent": "#FF5A4F",
    "brand_tint": "#382124",
    "brand_tint_hover": "#45272B",
    "brand_border_soft": "#783231",
    "brand_disabled_bg": "#3A2224",
    "brand_disabled_border": "#4D2C2F",
    "brand_disabled_fg": "#9A7A7D",
    # Typography
    "text_primary": "#F5F5F7",
    "text_secondary": "#A0A5B2",
    "text_disabled": "#696E7B",
    "text_on_brand": "#FFFFFF",
    # Semantic colors & tinted chip surfaces
    "semantic_success": "#2ECC71",
    "semantic_success_bg": "#182A21",
    "semantic_warning": "#F5A623",
    "semantic_warning_bg": "#2E2517",
    "semantic_danger_fg": "#FF9AA2",
    "semantic_danger_border": "#7A3D47",
    "semantic_danger_bg": "#2D1A1F",
    "semantic_danger_hover": "#341D23",
    "semantic_danger_pressed": "#3F222A",
    "semantic_info": "#60A5FA",
    "semantic_info_bg": "#172536",
    # Log colors
    "log_info": "#CFDBED",
    "log_success": "#7DE5A7",
    "log_warning": "#FFD58A",
    "log_error": "#FF9CAA",
}

LIGHT_TOKENS: dict[str, str] = {
    "name": "light",
    # Core surfaces
    "bg_app": "#F5F6F8",
    "bg_sidebar": "#FFFFFF",
    "bg_surface": "#FFFFFF",
    "bg_elevated": "#F0F1F4",
    "bg_hover": "#E6E8EE",
    "bg_pressed": "#DCE0E8",
    "bg_alt_row": "#F9FAFB",
    "bg_player": "#121318",
    "border_subtle": "#E4E7EC",
    "border_strong": "#CBD0DA",
    # Brand red accents
    "brand_primary": "#E53935",
    "brand_primary_hover": "#D32F2F",
    "brand_primary_pressed": "#B71C1C",
    "brand_accent": "#D92D20",
    "brand_tint": "#FDECEC",
    "brand_tint_hover": "#FCD8D8",
    "brand_border_soft": "#F3B4B3",
    "brand_disabled_bg": "#F4D4D4",
    "brand_disabled_border": "#EBC2C2",
    "brand_disabled_fg": "#A87C7C",
    # Typography
    "text_primary": "#17181C",
    "text_secondary": "#667085",
    "text_disabled": "#98A2B3",
    "text_on_brand": "#FFFFFF",
    # Semantic colors & tinted chip surfaces
    "semantic_success": "#12804A",
    "semantic_success_bg": "#E7F7EE",
    "semantic_warning": "#B56A00",
    "semantic_warning_bg": "#FEF4E4",
    "semantic_danger_fg": "#C62828",
    "semantic_danger_border": "#F1B5B5",
    "semantic_danger_bg": "#FDECEC",
    "semantic_danger_hover": "#FCDADA",
    "semantic_danger_pressed": "#F9C6C6",
    "semantic_info": "#1D68C4",
    "semantic_info_bg": "#EAF2FD",
    # Log colors
    "log_info": "#344054",
    "log_success": "#12804A",
    "log_warning": "#B56A00",
    "log_error": "#C62828",
}

_POPPINS_LOADED = False
_POPPINS_FAMILY = ""
_STYLESHEET_CACHE: dict[tuple[str, str], str] = {}


def load_bundled_fonts() -> str:
    """Load official bundled Poppins TTF weights into QFontDatabase once and return the active UI font family."""
    global _POPPINS_LOADED, _POPPINS_FAMILY
    if _POPPINS_LOADED and _POPPINS_FAMILY:
        return _POPPINS_FAMILY
    font_dir = ASSETS_DIR / "fonts" / "poppins"
    loaded_families: list[str] = []
    if QApplication.instance() is not None and font_dir.is_dir():
        for filename in (
            "Poppins-Regular.ttf",
            "Poppins-Medium.ttf",
            "Poppins-SemiBold.ttf",
            "Poppins-Bold.ttf",
        ):
            ttf = font_dir / filename
            if ttf.is_file():
                font_id = QFontDatabase.addApplicationFont(str(ttf))
                if font_id >= 0:
                    loaded_families.extend(QFontDatabase.applicationFontFamilies(font_id))
    if loaded_families:
        _POPPINS_FAMILY = loaded_families[0]
        _POPPINS_LOADED = True
        return _POPPINS_FAMILY
    return "Helvetica Neue" if sys.platform == "darwin" else "Segoe UI"


def _icon_url(filename: str) -> str:
    path = (ASSETS_DIR / "icons" / filename).resolve()
    return path.as_posix()


def tokens_for(resolved: ResolvedTheme) -> dict[str, str]:
    return LIGHT_TOKENS if resolved == "light" else DARK_TOKENS


def build_palette(resolved: ResolvedTheme) -> QPalette:
    t = tokens_for(resolved)
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(t["bg_app"]))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(t["text_primary"]))
    palette.setColor(QPalette.ColorRole.Base, QColor(t["bg_elevated"]))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(t["bg_alt_row"]))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(t["bg_surface"]))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(t["text_primary"]))
    palette.setColor(QPalette.ColorRole.Text, QColor(t["text_primary"]))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(t["text_secondary"]))
    palette.setColor(QPalette.ColorRole.Button, QColor(t["bg_elevated"]))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(t["text_primary"]))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(t["brand_primary"]))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(t["text_on_brand"]))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(t["text_disabled"]))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(t["text_disabled"]))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, QColor(t["text_disabled"]))
    return palette


def build_theme_stylesheet(resolved: ResolvedTheme = "dark", font_family: str | None = None) -> str:
    family = font_family or load_bundled_fonts()
    cache_key = (resolved, family)
    if cache_key in _STYLESHEET_CACHE:
        return _STYLESHEET_CACHE[cache_key]

    t = tokens_for(resolved)
    suffix = "light" if resolved == "light" else "dark"
    chev_down = _icon_url(f"chevron-down-{suffix}.svg")
    chev_up = _icon_url(f"chevron-up-{suffix}.svg")
    check_svg = _icon_url("check-white.svg")
    fallback_sans = '"Helvetica Neue", "Arial"' if sys.platform == "darwin" else '"Segoe UI", "Arial"'
    mono_font = '"Menlo", "Consolas", "Courier New"' if sys.platform == "darwin" else '"Consolas", "Menlo", "Courier New"'

    qss = f"""
* {{
    font-family: "{family}", {fallback_sans};
    font-size: 13px;
    font-weight: 400;
    color: {t["text_primary"]};
}}
QMainWindow, QWidget, QDialog {{
    background: {t["bg_app"]};
}}
QLabel, QCheckBox, QRadioButton {{
    background: transparent;
}}
QScrollArea, QScrollArea > QWidget > QWidget {{
    background: transparent;
}}
QFrame#sidebar {{
    background: {t["bg_sidebar"]};
    border-right: 1px solid {t["border_subtle"]};
}}
QLabel#brand {{
    background: transparent;
    font-size: 17px;
    font-weight: 600;
    color: {t["text_primary"]};
    padding: 4px;
}}
QLabel#pageTitle {{
    font-size: 26px;
    font-weight: 600;
    color: {t["text_primary"]};
}}
QLabel#sectionHeading {{
    font-size: 18px;
    font-weight: 600;
    color: {t["text_primary"]};
}}
QLabel#sectionTitle, QLabel#cardTitle {{
    font-size: 15px;
    font-weight: 600;
    color: {t["text_primary"]};
}}
QLabel#muted {{
    font-size: 12px;
    color: {t["text_secondary"]};
}}
QLabel#helperText {{
    font-size: 11px;
    color: {t["text_secondary"]};
}}
QLabel#metricValue {{
    font-size: 28px;
    font-weight: 600;
    color: {t["text_primary"]};
}}
QFrame#divider {{
    background: {t["border_subtle"]};
    max-height: 1px;
    min-height: 1px;
    border: none;
}}
QFrame#card, QGroupBox, QFrame#videoPreview {{
    background: {t["bg_surface"]};
    border: 1px solid {t["border_subtle"]};
    border-radius: 12px;
}}
QFrame#card QStackedWidget, QFrame#card QStackedWidget > QWidget,
QGroupBox QStackedWidget, QGroupBox QStackedWidget > QWidget {{
    background: transparent;
}}
QFrame#videoPreview QLabel, QWidget#videoPreviewSkeleton {{
    background: transparent;
}}
QLabel#projectNameLabel {{
    background: transparent;
}}
QLabel#videoPreviewStatus {{
    color: {t["brand_accent"]};
    font-size: 12px;
    font-weight: 600;
}}
QLabel#videoPreviewTitle {{
    color: {t["text_primary"]};
    font-size: 15px;
    font-weight: 600;
}}
QLabel#videoPreviewChannel {{
    color: {t["text_primary"]};
    font-weight: 500;
}}
QLabel#videoPreviewDetail {{
    color: {t["text_secondary"]};
    font-size: 12px;
}}
QFrame#videoPreviewBar {{
    background: {t["bg_elevated"]};
    border-radius: 5px;
}}
QGroupBox {{
    margin-top: 14px;
    padding: 20px 18px 16px;
    font-weight: 600;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 14px;
    padding: 0 6px;
    color: {t["text_secondary"]};
    background: {t["bg_app"]};
}}

/* Buttons */
QPushButton {{
    background: {t["bg_elevated"]};
    border: 1px solid {t["border_subtle"]};
    border-radius: 8px;
    padding: 7px 16px;
    min-height: 20px;
    font-size: 13px;
    font-weight: 500;
    color: {t["text_primary"]};
}}
QPushButton:hover {{
    background: {t["bg_hover"]};
    border-color: {t["border_strong"]};
}}
QPushButton:pressed {{
    background: {t["bg_pressed"]};
}}
QPushButton:disabled {{
    color: {t["text_disabled"]};
    background: {t["bg_surface"]};
    border-color: {t["border_subtle"]};
}}
QPushButton#primary {{
    background: {t["brand_primary"]};
    border: 1px solid {t["brand_primary"]};
    color: {t["text_on_brand"]};
    font-weight: 600;
    padding: 8px 18px;
}}
QPushButton#primary:hover {{
    background: {t["brand_primary_hover"]};
    border-color: {t["brand_primary_hover"]};
}}
QPushButton#primary:pressed {{
    background: {t["brand_primary_pressed"]};
    border-color: {t["brand_primary_pressed"]};
}}
QPushButton#primary:disabled {{
    background: {t["brand_disabled_bg"]};
    border-color: {t["brand_disabled_border"]};
    color: {t["brand_disabled_fg"]};
}}
QPushButton#ghost {{
    background: transparent;
    border: 1px solid transparent;
    color: {t["text_secondary"]};
    padding: 6px 12px;
}}
QPushButton#ghost:hover {{
    background: {t["bg_elevated"]};
    color: {t["text_primary"]};
}}
QPushButton#danger {{
    color: {t["semantic_danger_fg"]};
    background: transparent;
    border: 1px solid {t["semantic_danger_border"]};
}}
QPushButton#danger:hover {{
    background: {t["semantic_danger_hover"]};
    border-color: {t["semantic_danger_fg"]};
}}
QPushButton#danger:pressed {{
    background: {t["semantic_danger_pressed"]};
}}
QPushButton#danger:disabled {{
    color: {t["text_disabled"]};
    border-color: {t["border_subtle"]};
}}
QPushButton#nav {{
    text-align: left;
    border: none;
    border-left: 3px solid transparent;
    border-radius: 8px;
    background: transparent;
    color: {t["text_secondary"]};
    padding: 9px 12px;
    font-size: 13px;
    font-weight: 500;
}}
QPushButton#nav:hover {{
    background: {t["bg_elevated"]};
    color: {t["text_primary"]};
}}
QPushButton#nav:checked {{
    background: {t["brand_tint"]};
    color: {t["text_primary"]};
    font-weight: 600;
    border-left: 3px solid {t["brand_primary"]};
}}
QPushButton#appearanceBtn {{
    background: transparent;
    border: 1px solid transparent;
    border-radius: 6px;
    padding: 5px 6px;
    min-height: 18px;
    font-size: 11px;
    font-weight: 500;
    color: {t["text_secondary"]};
}}
QPushButton#appearanceBtn:hover {{
    background: {t["bg_hover"]};
    color: {t["text_primary"]};
}}
QPushButton#appearanceBtn:checked {{
    background: {t["bg_surface"]};
    border-color: {t["border_subtle"]};
    color: {t["text_primary"]};
    font-weight: 600;
}}
QFrame#appearanceBar {{
    background: {t["bg_elevated"]};
    border: 1px solid {t["border_subtle"]};
    border-radius: 8px;
}}
QPushButton#videoPreviewRetry {{
    padding: 6px 12px;
}}

/* Focus indicators */
QPushButton:focus, QPushButton#primary:focus, QPushButton#nav:focus, QPushButton#danger:focus,
QTableWidget:focus {{
    border: 1px solid {t["brand_accent"]};
}}

/* Text inputs, SpinBoxes, ComboBoxes */
QLineEdit, QPlainTextEdit {{
    background: {t["bg_elevated"]};
    border: 1px solid {t["border_subtle"]};
    border-radius: 8px;
    padding: 7px 12px;
    min-height: 20px;
    color: {t["text_primary"]};
    selection-background-color: {t["brand_primary"]};
    selection-color: {t["text_on_brand"]};
}}
QLineEdit:focus, QPlainTextEdit:focus {{
    border-color: {t["brand_accent"]};
}}
QLineEdit:disabled, QPlainTextEdit:disabled {{
    color: {t["text_disabled"]};
    background: {t["bg_surface"]};
}}
QPlainTextEdit#logView {{
    font-family: {mono_font};
    font-size: 12px;
}}

QSpinBox, QDoubleSpinBox {{
    background: {t["bg_elevated"]};
    border: 1px solid {t["border_subtle"]};
    border-radius: 8px;
    padding: 7px 32px 7px 12px;
    min-height: 20px;
    color: {t["text_primary"]};
    selection-background-color: {t["brand_primary"]};
    selection-color: {t["text_on_brand"]};
}}
QSpinBox:focus, QDoubleSpinBox:focus {{
    border-color: {t["brand_accent"]};
}}
QSpinBox:disabled, QDoubleSpinBox:disabled {{
    color: {t["text_disabled"]};
    background: {t["bg_surface"]};
}}
QSpinBox::up-button, QDoubleSpinBox::up-button {{
    subcontrol-origin: border;
    subcontrol-position: top right;
    width: 26px;
    border-left: 1px solid {t["border_subtle"]};
    border-bottom: 1px solid {t["border_subtle"]};
    border-top-right-radius: 7px;
    background: transparent;
}}
QSpinBox::down-button, QDoubleSpinBox::down-button {{
    subcontrol-origin: border;
    subcontrol-position: bottom right;
    width: 26px;
    border-left: 1px solid {t["border_subtle"]};
    border-bottom-right-radius: 7px;
    background: transparent;
}}
QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {{
    background: {t["bg_hover"]};
}}
QSpinBox::up-button:pressed, QDoubleSpinBox::up-button:pressed,
QSpinBox::down-button:pressed, QDoubleSpinBox::down-button:pressed {{
    background: {t["bg_pressed"]};
}}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{
    image: url("{chev_up}");
    width: 11px;
    height: 11px;
}}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{
    image: url("{chev_down}");
    width: 11px;
    height: 11px;
}}

QComboBox {{
    background: {t["bg_elevated"]};
    border: 1px solid {t["border_subtle"]};
    border-radius: 8px;
    padding: 7px 32px 7px 12px;
    min-height: 20px;
    color: {t["text_primary"]};
    selection-background-color: {t["brand_primary"]};
    selection-color: {t["text_on_brand"]};
}}
QComboBox:hover {{
    border-color: {t["border_strong"]};
}}
QComboBox:focus {{
    border-color: {t["brand_accent"]};
}}
QComboBox:disabled {{
    color: {t["text_disabled"]};
    background: {t["bg_surface"]};
}}
QComboBox::drop-down {{
    subcontrol-origin: border;
    subcontrol-position: top right;
    width: 28px;
    border: none;
    border-left: 1px solid {t["border_subtle"]};
    border-top-right-radius: 7px;
    border-bottom-right-radius: 7px;
    background: transparent;
}}
QComboBox::down-arrow {{
    image: url("{chev_down}");
    width: 12px;
    height: 12px;
}}
QComboBox QAbstractItemView {{
    background: {t["bg_surface"]};
    color: {t["text_primary"]};
    selection-background-color: {t["brand_tint"]};
    selection-color: {t["text_primary"]};
    border: 1px solid {t["border_strong"]};
    border-radius: 8px;
    padding: 4px;
    outline: none;
}}
QComboBox QAbstractItemView::item {{
    padding: 6px 10px;
    border-radius: 6px;
    min-height: 22px;
}}

/* Checkboxes & Radio buttons */
QCheckBox, QRadioButton {{
    spacing: 8px;
    color: {t["text_primary"]};
}}
QCheckBox:disabled, QRadioButton:disabled {{
    color: {t["text_disabled"]};
}}
QCheckBox::indicator {{
    width: 16px;
    height: 16px;
    border: 1px solid {t["border_strong"]};
    border-radius: 4px;
    background: {t["bg_elevated"]};
}}
QCheckBox::indicator:hover {{
    border-color: {t["brand_accent"]};
}}
QCheckBox::indicator:checked {{
    background: {t["brand_primary"]};
    border-color: {t["brand_primary"]};
    image: url("{check_svg}");
}}
QCheckBox::indicator:disabled {{
    background: {t["bg_surface"]};
    border-color: {t["border_subtle"]};
}}

/* Tables */
QTableWidget {{
    background: {t["bg_surface"]};
    alternate-background-color: {t["bg_alt_row"]};
    border: 1px solid {t["border_subtle"]};
    border-radius: 10px;
    gridline-color: {t["border_subtle"]};
    color: {t["text_primary"]};
}}
QHeaderView::section {{
    background: {t["bg_elevated"]};
    color: {t["text_secondary"]};
    border: none;
    border-bottom: 1px solid {t["border_subtle"]};
    padding: 9px 12px;
    font-size: 12px;
    font-weight: 600;
}}

/* Progress bars & Sliders */
QProgressBar {{
    background: {t["bg_elevated"]};
    border: none;
    border-radius: 5px;
    height: 8px;
    text-align: center;
    color: transparent;
}}
QProgressBar::chunk {{
    background: {t["brand_primary"]};
    border-radius: 5px;
}}
QSlider::groove:horizontal {{
    height: 5px;
    background: {t["border_subtle"]};
    border-radius: 2px;
}}
QSlider::sub-page:horizontal {{
    height: 5px;
    background: {t["brand_primary"]};
    border-radius: 2px;
}}
QSlider::handle:horizontal {{
    width: 14px;
    height: 14px;
    margin: -5px 0;
    background: {t["brand_primary"]};
    border: 2px solid {t["bg_surface"]};
    border-radius: 7px;
}}
QSplitter::handle {{
    background: transparent;
}}
QSplitter::handle:hover {{
    background: {t["border_subtle"]};
    border-radius: 2px;
}}

/* Transfer & Speech progress */
QWidget#downloadView, QWidget#downloadView QLabel {{
    background: transparent;
}}
QFrame#transferIndicator {{
    background: {t["brand_accent"]};
    border-radius: 3px;
}}
QLabel#transferStatus {{
    color: {t["text_primary"]};
    font-weight: 600;
}}
QLabel#transferSummary, QLabel#transferMetric, QLabel#transferPercent {{
    font-family: {mono_font};
}}
QLabel#transferSummary {{
    color: {t["text_secondary"]};
    font-size: 12px;
}}
QLabel#transferMetricLabel {{
    color: {t["text_secondary"]};
    font-size: 11px;
}}
QLabel#transferMetric {{
    color: {t["text_primary"]};
    font-size: 13px;
}}
QLabel#transferPercent {{
    min-width: 84px;
    color: {t["brand_accent"]};
    font-size: 22px;
    font-weight: 600;
}}
QProgressBar#downloadTransfer {{
    background: {t["bg_elevated"]};
    border-radius: 4px;
}}
QProgressBar#downloadTransfer::chunk {{
    background: {t["brand_primary"]};
    border-radius: 4px;
}}
QPushButton#cancelDownload {{
    color: {t["semantic_danger_fg"]};
    background: transparent;
    border: 1px solid {t["semantic_danger_border"]};
    padding: 6px 12px;
}}
QPushButton#cancelDownload:hover {{
    background: {t["semantic_danger_hover"]};
    border-color: {t["semantic_danger_fg"]};
}}

/* Caption Template cards */
QFrame#captionCard {{
    background: {t["bg_surface"]};
    border: 1px solid {t["border_subtle"]};
    border-radius: 12px;
}}
QFrame#captionCard:hover {{
    border-color: {t["border_strong"]};
}}
QFrame#captionCard[playing="true"] {{
    border-color: {t["brand_border_soft"]};
}}
QFrame#captionCard[selected="true"] {{
    background: {t["brand_tint"]};
    border-color: {t["brand_primary"]};
}}
QLabel#captionTitle {{
    background: transparent;
    color: {t["text_primary"]};
    font-size: 15px;
    font-weight: 600;
}}
QLabel#captionDescription {{
    background: transparent;
    color: {t["text_secondary"]};
    font-size: 12px;
}}
QProgressBar#captionPreviewProgress {{
    background: {t["bg_elevated"]};
    border-radius: 1px;
}}
QProgressBar#captionPreviewProgress::chunk {{
    background: {t["brand_accent"]};
    border-radius: 1px;
}}
QPushButton#captionPreviewPlay, QPushButton#captionPreviewReplay, QPushButton#captionSelect {{
    padding: 6px 10px;
}}
QPushButton#captionSelect[selected="true"] {{
    background: {t["brand_tint_hover"]};
    color: {t["text_primary"]};
    border-color: {t["brand_primary"]};
}}

/* Scrollbars */
QScrollBar:vertical {{
    width: 8px;
    background: transparent;
}}
QScrollBar::handle:vertical {{
    background: {t["border_strong"]};
    min-height: 28px;
    border-radius: 4px;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
    background: transparent;
    height: 0px;
}}
QScrollBar:horizontal {{
    height: 8px;
    background: transparent;
}}
QScrollBar::handle:horizontal {{
    background: {t["border_strong"]};
    min-width: 28px;
    border-radius: 4px;
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal,
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
    background: transparent;
    width: 0px;
}}

/* Summary pills, Chips, Cards, Tabs */
QFrame#summaryPill {{
    background: {t["bg_surface"]};
    border: 1px solid {t["border_subtle"]};
    border-radius: 10px;
}}
QLabel#summaryValue {{
    font-size: 16px;
    font-weight: 600;
    color: {t["text_primary"]};
}}
QFrame#clipLibraryCard, QFrame#queueRowCard {{
    background: {t["bg_surface"]};
    border: 1px solid {t["border_subtle"]};
    border-radius: 10px;
}}
QFrame#clipLibraryCard:hover, QFrame#queueRowCard:hover {{
    background: {t["bg_alt_row"]};
    border-color: {t["border_strong"]};
}}
QFrame#clipLibraryCard[selected="true"], QFrame#queueRowCard[selected="true"] {{
    background: {t["brand_tint"]};
    border: 1px solid {t["brand_border_soft"]};
}}
QLabel#scorePill {{
    background: {t["bg_elevated"]};
    border: 1px solid {t["border_subtle"]};
    border-radius: 6px;
    padding: 3px 8px;
    font-size: 12px;
    font-weight: 600;
    color: {t["text_primary"]};
}}
QLabel#statusChip {{
    background: {t["bg_elevated"]};
    border-radius: 6px;
    padding: 3px 8px;
    font-size: 11px;
    font-weight: 500;
    color: {t["text_secondary"]};
}}
QLabel#statusChip[tone="success"] {{
    background: {t["semantic_success_bg"]};
    color: {t["semantic_success"]};
    font-weight: 600;
}}
QLabel#statusChip[tone="warning"] {{
    background: {t["semantic_warning_bg"]};
    color: {t["semantic_warning"]};
    font-weight: 600;
}}
QLabel#statusChip[tone="danger"] {{
    background: {t["semantic_danger_bg"]};
    color: {t["semantic_danger_fg"]};
    font-weight: 600;
}}
QLabel#statusChip[tone="info"] {{
    background: {t["semantic_info_bg"]};
    color: {t["semantic_info"]};
    font-weight: 600;
}}
QPushButton#viewToggle {{
    background: {t["bg_elevated"]};
    border: 1px solid {t["border_subtle"]};
    padding: 6px 10px;
    color: {t["text_secondary"]};
}}
QPushButton#viewToggle:checked {{
    background: {t["brand_tint"]};
    border-color: {t["brand_primary"]};
    color: {t["text_primary"]};
}}
QPushButton#sourceTab {{
    background: {t["bg_elevated"]};
    border: 1px solid {t["border_subtle"]};
    border-radius: 8px;
    padding: 7px 14px;
    color: {t["text_secondary"]};
    font-weight: 500;
}}
QPushButton#sourceTab:checked {{
    background: {t["brand_tint"]};
    border-color: {t["brand_primary"]};
    color: {t["text_primary"]};
    font-weight: 600;
}}
QPushButton#filterTab {{
    background: transparent;
    border: 1px solid transparent;
    border-radius: 8px;
    padding: 4px 8px;
    color: {t["text_secondary"]};
    font-weight: 500;
}}
QPushButton#filterTab:hover {{
    background: {t["bg_elevated"]};
    color: {t["text_primary"]};
}}
QPushButton#filterTab:checked {{
    background: {t["brand_tint"]};
    border-color: {t["brand_border_soft"]};
    color: {t["text_primary"]};
    font-weight: 600;
}}
QFrame#playerStage {{
    background: {t["bg_player"]};
    border: 1px solid {t["border_subtle"]};
    border-radius: 10px;
}}
QPushButton#iconButton {{
    background: {t["bg_elevated"]};
    border: 1px solid {t["border_subtle"]};
    border-radius: 8px;
    padding: 6px 10px;
}}
QPushButton#iconButton:hover {{
    background: {t["bg_hover"]};
    border-color: {t["border_strong"]};
}}
QPushButton#iconButton::menu-indicator {{
    subcontrol-origin: padding;
    subcontrol-position: right center;
    right: 6px;
}}

/* Menus & Tooltips */
QMenu {{
    background: {t["bg_surface"]};
    border: 1px solid {t["border_strong"]};
    border-radius: 8px;
    padding: 6px;
}}
QMenu::item {{
    padding: 7px 14px;
    border-radius: 6px;
    color: {t["text_primary"]};
}}
QMenu::item:selected {{
    background: {t["bg_hover"]};
}}
QMenu::separator {{
    height: 1px;
    background: {t["border_subtle"]};
    margin: 4px 8px;
}}
QToolTip {{
    background: {t["bg_surface"]};
    color: {t["text_primary"]};
    border: 1px solid {t["border_strong"]};
    border-radius: 6px;
    padding: 6px 10px;
    font-size: 12px;
}}
"""
    _STYLESHEET_CACHE[cache_key] = qss
    return qss


def detect_system_theme() -> ResolvedTheme:
    """Detect native OS light/dark appearance via Qt 6.5+ QStyleHints."""
    app = QApplication.instance()
    if app is not None:
        try:
            scheme = app.styleHints().colorScheme()
            if scheme == Qt.ColorScheme.Light:
                return "light"
            if scheme == Qt.ColorScheme.Dark:
                return "dark"
        except Exception:
            pass
    return "dark"


class SectionCard(QFrame):
    """Modern card container with clean internal title header, replacing awkward QGroupBox borders."""

    def __init__(self, title: str = "", subtitle: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent, objectName="card")
        self.root_layout = QVBoxLayout(self)
        self.root_layout.setContentsMargins(18, 16, 18, 16)
        self.root_layout.setSpacing(12)
        if title:
            header = QVBoxLayout()
            header.setContentsMargins(0, 0, 0, 2)
            header.setSpacing(2)
            self.title_label = QLabel(title, objectName="cardTitle")
            self.title_label.setWordWrap(True)
            header.addWidget(self.title_label)
            if subtitle:
                self.subtitle_label = QLabel(subtitle, objectName="muted")
                self.subtitle_label.setWordWrap(True)
                header.addWidget(self.subtitle_label)
            else:
                self.subtitle_label = None
            self.root_layout.addLayout(header)
        else:
            self.title_label = None
            self.subtitle_label = None

    def body_layout(self) -> QVBoxLayout:
        return self.root_layout


class ThemeManager(QObject):
    """Application-wide ThemeManager supporting Light, Dark, and System appearance modes."""

    theme_changed = Signal(str)  # Emits resolved theme: "dark" | "light"

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._mode: AppearanceMode = "dark"
        self._resolved: ResolvedTheme = "dark"
        self._font_family: str = ""
        self._hooked_system = False

    @property
    def mode(self) -> AppearanceMode:
        return self._mode

    @property
    def resolved(self) -> ResolvedTheme:
        return self._resolved

    @property
    def tokens(self) -> dict[str, str]:
        return tokens_for(self._resolved)

    @property
    def font_family(self) -> str:
        return self._font_family or load_bundled_fonts()

    def _ensure_system_hook(self) -> None:
        if self._hooked_system:
            return
        app = QApplication.instance()
        if app is not None:
            try:
                app.styleHints().colorSchemeChanged.connect(self._on_system_scheme_changed)
                self._hooked_system = True
            except Exception:
                pass

    def _on_system_scheme_changed(self, _scheme: object) -> None:
        if self._mode == "system":
            self.apply("system")

    def apply(self, mode: str = "dark") -> ResolvedTheme:
        normalized: AppearanceMode = mode if mode in {"dark", "light", "system"} else "dark"  # type: ignore[assignment]
        self._mode = normalized
        self._ensure_system_hook()
        resolved: ResolvedTheme = detect_system_theme() if normalized == "system" else normalized  # type: ignore[assignment]
        self._resolved = resolved
        self._font_family = load_bundled_fonts()

        app = QApplication.instance()
        if app is not None:
            base_font = QFont(self._font_family)
            base_font.setPixelSize(13)
            base_font.setWeight(QFont.Weight.Normal)
            app.setFont(base_font)
            app.setPalette(build_palette(resolved))
            app.setStyleSheet(build_theme_stylesheet(resolved, self._font_family))
        self.theme_changed.emit(resolved)
        return resolved


_GLOBAL_THEME_MANAGER: ThemeManager | None = None


def theme_manager() -> ThemeManager:
    global _GLOBAL_THEME_MANAGER
    if _GLOBAL_THEME_MANAGER is None:
        _GLOBAL_THEME_MANAGER = ThemeManager()
    return _GLOBAL_THEME_MANAGER
