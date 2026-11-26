"""Centralized design tokens, stylesheet facade, global wheel protection, and shared layout helpers."""
from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QDial,
    QLineEdit,
    QPlainTextEdit,
    QSlider,
    QTextEdit,
    QWidget,
)

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

_WHEEL_GUARD_KEY = "_autoclip_wheel_guard"


def _resolve_wheel_protected_control(watched: QObject) -> QWidget | None:
    """Return the wheel-protected control when a wheel event targets one directly or via its editor child."""
    if not isinstance(watched, QWidget):
        return None
    if isinstance(watched, (QComboBox, QAbstractSpinBox, QSlider, QDial)):
        ctrl: QWidget = watched
    elif isinstance(watched, QLineEdit) and isinstance(watched.parentWidget(), (QComboBox, QAbstractSpinBox)):
        ctrl = watched.parentWidget()
    else:
        return None
    if bool(ctrl.property("allow_wheel_scroll")):
        return None
    return ctrl


def _find_scrollable_ancestor(control: QWidget, event: QWheelEvent) -> QAbstractScrollArea | None:
    """Find the enclosing page/container scroll area without hijacking unrelated text editors."""
    wants_horiz = (
        abs(event.pixelDelta().x()) > abs(event.pixelDelta().y())
        or abs(event.angleDelta().x()) > abs(event.angleDelta().y())
    )
    first_area: QAbstractScrollArea | None = None
    cursor = control.parentWidget()
    while cursor is not None:
        if isinstance(cursor, QAbstractScrollArea) and not isinstance(cursor, (QPlainTextEdit, QTextEdit)):
            if first_area is None:
                first_area = cursor
            bar = cursor.horizontalScrollBar() if wants_horiz else cursor.verticalScrollBar()
            if bar is not None and bar.maximum() > bar.minimum():
                return cursor
        cursor = cursor.parentWidget()
    return first_area


class WheelScrollGuard(QObject):
    """Application-level event filter blocking incidental wheel/trackpad edits while keeping page scrolling smooth."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._forwarding = False

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if self._forwarding or event.type() != QEvent.Type.Wheel:
            return super().eventFilter(watched, event)

        control = _resolve_wheel_protected_control(watched)
        if control is None or not isinstance(event, QWheelEvent):
            return super().eventFilter(watched, event)

        target_scroll = _find_scrollable_ancestor(control, event)
        if target_scroll is not None:
            viewport = target_scroll.viewport()
            if viewport is not None:
                global_pos = event.globalPosition()
                mapped_local = QPointF(watched.mapTo(viewport, event.position().toPoint()))
                forwarded = QWheelEvent(
                    mapped_local,
                    global_pos,
                    event.pixelDelta(),
                    event.angleDelta(),
                    event.buttons(),
                    event.modifiers(),
                    event.phase(),
                    event.inverted(),
                    event.source(),
                    event.pointingDevice(),
                )
                self._forwarding = True
                try:
                    QApplication.sendEvent(viewport, forwarded)
                finally:
                    self._forwarding = False
                if forwarded.isAccepted():
                    event.accept()
                    return True

        event.ignore()
        return True


def install_global_wheel_protection(app: QApplication | None = None) -> WheelScrollGuard | None:
    """Install the centralized wheel guard idempotently on the active QApplication."""
    target_app = app or QApplication.instance()
    if target_app is None:
        return None
    existing = getattr(target_app, _WHEEL_GUARD_KEY, None)
    if isinstance(existing, WheelScrollGuard):
        return existing
    guard = WheelScrollGuard(target_app)
    target_app.installEventFilter(guard)
    setattr(target_app, _WHEEL_GUARD_KEY, guard)
    return guard


def protect_from_wheel(widget: QWidget) -> QWidget:
    """Ensure global wheel protection is active and preserve keyboard-accessible focus behavior."""
    install_global_wheel_protection()
    if isinstance(widget, (QComboBox, QAbstractSpinBox, QSlider, QDial)):
        if widget.focusPolicy() == Qt.FocusPolicy.WheelFocus:
            widget.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    return widget

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
    """Configure an input or selector widget to expand responsively without clipping or accidental wheel changes."""
    from PySide6.QtWidgets import QSizePolicy

    widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    if min_width > 0:
        widget.setMinimumWidth(min_width)
    if isinstance(widget, QComboBox):
        widget.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        widget.setMinimumContentsLength(min_contents_length)
        view = widget.view()
        if view is not None:
            view.setTextElideMode(Qt.TextElideMode.ElideNone)
    protect_from_wheel(widget)
    return widget
