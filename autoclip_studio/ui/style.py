"""Centralized design tokens, stylesheet facade, and shared layout helpers for AutoClip Studio."""
from __future__ import annotations

from .theme import (
    DARK_TOKENS,
    LIGHT_TOKENS,
    RADIUS,
    SPACING,
    TYPOGRAPHY,
    SectionCard,
    ThemeManager,
    build_palette,
    build_theme_stylesheet,
    load_bundled_fonts,
    theme_manager,
    tokens_for,
)

# Preserved export name for backward compatibility with tests and callers.
TOKENS: dict[str, str] = DARK_TOKENS


def pick_ui_font() -> str:
    """Return the active bundled Poppins UI font family (with OS sans fallback)."""
    return load_bundled_fonts()


def build_stylesheet(tokens: dict[str, str] = DARK_TOKENS, font_family: str | None = None) -> str:
    resolved = "light" if tokens.get("name") == "light" else "dark"
    return build_theme_stylesheet(resolved, font_family=font_family)


STYLESHEET = build_theme_stylesheet("dark")


def configure_form_layout(form, *, horizontal_spacing: int = 16, vertical_spacing: int = 12):
    """Apply consistent, cross-platform responsive QFormLayout behavior on macOS and Windows."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QFormLayout

    form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
    form.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
    form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
    form.setHorizontalSpacing(horizontal_spacing)
    form.setVerticalSpacing(vertical_spacing)
    return form


def configure_input(widget, *, min_width: int = 0, min_contents_length: int = 16):
    """Configure an input or selector widget to expand responsively without clipping or blowing out layouts."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QComboBox, QSizePolicy

    widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    if min_width > 0:
        widget.setMinimumWidth(min_width)
    if isinstance(widget, QComboBox):
        widget.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        widget.setMinimumContentsLength(min_contents_length)
        view = widget.view()
        if view is not None:
            view.setTextElideMode(Qt.TextElideMode.ElideNone)
    return widget
