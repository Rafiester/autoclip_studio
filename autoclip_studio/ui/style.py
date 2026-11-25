"""Centralized design tokens and Qt stylesheet for AutoClip Studio."""
from __future__ import annotations

TOKENS: dict[str, str] = {
    # Core brand & surfaces
    "bg_app": "#101116",          # Obsidian Black
    "bg_sidebar": "#14151C",
    "bg_surface": "#191B22",      # Cards & panels
    "bg_elevated": "#22252E",     # Inputs & raised surfaces
    "bg_hover": "#2B2F3A",
    "bg_pressed": "#15171E",
    "border_subtle": "#30343E",
    "border_strong": "#434856",
    # Brand red accents
    "brand_primary": "#E53935",   # Modern Red
    "brand_primary_hover": "#D32F2F",
    "brand_primary_pressed": "#B71C1C",
    "brand_accent": "#FF5A4F",    # Accent Red
    "brand_tint": "#2A181B",
    "brand_tint_hover": "#361E22",
    "brand_border_soft": "#6B2A29",
    # Typography
    "text_primary": "#F5F5F7",    # Soft White
    "text_secondary": "#A3A8B5",  # Muted text
    "text_disabled": "#696E7B",
    # Semantic (kept distinct from brand primary actions)
    "semantic_success": "#2ECC71",
    "semantic_warning": "#F5A623",
    "semantic_danger_fg": "#FF9AA2",
    "semantic_danger_border": "#7A3D47",
    "semantic_danger_hover": "#2D1A1F",
    "semantic_danger_pressed": "#3C2027",
    "semantic_info": "#5BA4CF",
}

def pick_ui_font() -> str:
    """Return the preferred installed UI font, falling back to Segoe UI."""
    preferred = ("Inter", "Geist", "Manrope", "Segoe UI")
    try:
        from PySide6.QtGui import QFontDatabase
        from PySide6.QtWidgets import QApplication

        if QApplication.instance() is not None:
            available = set(QFontDatabase.families())
            for family in preferred:
                if family in available:
                    return family
    except Exception:
        pass
    return "Segoe UI"


def build_stylesheet(tokens: dict[str, str] = TOKENS, font_family: str | None = None) -> str:
    t = tokens
    ui_font = font_family or pick_ui_font()
    return f"""
* {{
    font-family: "{ui_font}";
    font-size: 13px;
    color: {t["text_primary"]};
}}
QMainWindow, QWidget {{
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
    font-size: 18px;
    font-weight: 700;
    color: {t["text_primary"]};
    padding: 4px 4px;
}}
QLabel#pageTitle {{
    font-size: 24px;
    font-weight: 700;
    color: {t["text_primary"]};
}}
QLabel#sectionTitle {{
    font-size: 15px;
    font-weight: 600;
    color: {t["text_primary"]};
}}
QLabel#muted {{
    color: {t["text_secondary"]};
}}
QLabel#metricValue {{
    font-size: 22px;
    font-weight: 700;
    color: {t["text_primary"]};
}}
QFrame#videoPreview {{
    background: {t["bg_surface"]};
    border: 1px solid {t["border_subtle"]};
    border-radius: 10px;
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
    font-weight: 700;
}}
QLabel#videoPreviewTitle {{
    color: {t["text_primary"]};
    font-size: 16px;
    font-weight: 700;
}}
QLabel#videoPreviewChannel {{
    color: {t["text_primary"]};
    font-weight: 600;
}}
QLabel#videoPreviewDetail {{
    color: {t["text_secondary"]};
}}
QFrame#videoPreviewBar {{
    background: {t["bg_elevated"]};
    border-radius: 5px;
}}
QPushButton#videoPreviewRetry {{
    padding: 6px 12px;
}}
QFrame#card, QGroupBox {{
    background: {t["bg_surface"]};
    border: 1px solid {t["border_subtle"]};
    border-radius: 12px;
}}
QGroupBox {{
    margin-top: 16px;
    padding: 20px 16px 16px;
    font-weight: 600;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 14px;
    padding: 0 6px;
    color: {t["text_secondary"]};
    background: {t["bg_app"]};
}}
QPushButton {{
    background: {t["bg_elevated"]};
    border: 1px solid {t["border_subtle"]};
    border-radius: 8px;
    padding: 8px 14px;
    font-weight: 500;
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
    border-color: {t["brand_accent"]};
    color: {t["text_primary"]};
    font-weight: 700;
    padding: 10px 18px;
}}
QPushButton#primary:hover {{
    background: {t["brand_primary_hover"]};
}}
QPushButton#primary:pressed {{
    background: {t["brand_primary_pressed"]};
}}
QPushButton#primary:disabled {{
    background: #3A2224;
    border-color: #4D2C2F;
    color: #9A7A7D;
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
    border-radius: 8px;
    background: transparent;
    color: {t["text_secondary"]};
    padding: 10px 12px;
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
QPushButton:focus, QPushButton#primary:focus, QPushButton#nav:focus, QPushButton#danger:focus,
QCheckBox:focus, QTableWidget:focus {{
    border: 2px solid {t["brand_accent"]};
}}
QLineEdit, QSpinBox, QComboBox, QPlainTextEdit {{
    background: {t["bg_elevated"]};
    border: 1px solid {t["border_subtle"]};
    border-radius: 8px;
    padding: 8px 10px;
    selection-background-color: {t["brand_primary"]};
}}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus, QPlainTextEdit:focus {{
    border-color: {t["brand_accent"]};
}}
QComboBox QAbstractItemView {{
    background: {t["bg_elevated"]};
    selection-background-color: {t["brand_tint"]};
    border: 1px solid {t["border_strong"]};
}}
QTableWidget {{
    background: {t["bg_surface"]};
    alternate-background-color: #1D2028;
    border: 1px solid {t["border_subtle"]};
    border-radius: 10px;
    gridline-color: {t["border_subtle"]};
}}
QHeaderView::section {{
    background: {t["bg_elevated"]};
    color: {t["text_secondary"]};
    border: none;
    border-bottom: 1px solid {t["border_subtle"]};
    padding: 9px 10px;
    font-weight: 600;
}}
QProgressBar {{
    background: {t["bg_elevated"]};
    border: none;
    border-radius: 5px;
    height: 10px;
    text-align: center;
    color: transparent;
}}
QProgressBar::chunk {{
    background: {t["brand_primary"]};
    border-radius: 5px;
}}
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
    font-family: "Consolas", "Courier New";
}}
QLabel#transferSummary {{
    color: {t["text_secondary"]};
}}
QLabel#transferMetricLabel {{
    color: {t["text_secondary"]};
    font-size: 12px;
}}
QLabel#transferMetric {{
    color: {t["text_primary"]};
    font-size: 14px;
}}
QLabel#transferPercent {{
    min-width: 88px;
    color: {t["brand_accent"]};
    font-size: 23px;
    font-weight: 700;
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
    padding: 6px 10px;
}}
QPushButton#cancelDownload:hover {{
    background: {t["semantic_danger_hover"]};
    border-color: {t["semantic_danger_fg"]};
}}
QPushButton#cancelDownload:pressed {{
    background: {t["semantic_danger_pressed"]};
    border-color: {t["semantic_danger_fg"]};
}}
QPushButton#cancelDownload:focus {{
    border-color: {t["semantic_danger_fg"]};
}}
QPushButton#cancelDownload:disabled {{
    border-color: {t["border_subtle"]};
    color: {t["text_disabled"]};
}}
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
    font-weight: 700;
}}
QLabel#captionDescription {{
    background: transparent;
    color: {t["text_secondary"]};
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
    padding: 6px 8px;
}}
QPushButton#captionPreviewReplay {{
    font-size: 15px;
}}
QPushButton#captionSelect[selected="true"] {{
    background: {t["brand_tint_hover"]};
    color: {t["text_primary"]};
    border-color: {t["brand_primary"]};
}}
QPushButton#captionSelect[selected="true"]:hover {{
    background: #422428;
}}
QPushButton#captionSelect[selected="true"]:focus {{
    border-color: {t["brand_accent"]};
}}
QCheckBox {{
    spacing: 8px;
}}
QCheckBox::indicator {{
    width: 17px;
    height: 17px;
}}
QScrollBar:vertical {{
    width: 9px;
    background: transparent;
}}
QScrollBar::handle:vertical {{
    background: {t["border_strong"]};
    min-height: 28px;
    border-radius: 4px;
}}
QSlider::groove:horizontal {{
    height: 5px;
    background: {t["bg_elevated"]};
    border-radius: 2px;
}}
QSlider::handle:horizontal {{
    width: 15px;
    margin: -5px 0;
    background: {t["brand_primary"]};
    border-radius: 7px;
}}
"""


STYLESHEET = build_stylesheet()
