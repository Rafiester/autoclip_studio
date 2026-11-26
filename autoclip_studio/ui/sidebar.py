"""Floating SaaS navigation sidebar, nested Settings tree, and Appearance preview cards."""
from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QPoint, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QLabel, QMenu, QPushButton,
    QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from .. import __version__
from ..paths import asset_path
from .icons import lucide_icon
from .theme import DARK_TOKENS, LIGHT_TOKENS, theme_manager


@dataclass(frozen=True, slots=True)
class NavRouteSpec:
    key: str
    label: str
    icon: str
    page_index: int


PRIMARY_NAV_ROUTES: tuple[NavRouteSpec, ...] = (
    NavRouteSpec("dashboard", "Dashboard", "layout-dashboard", 0),
    NavRouteSpec("new_project", "New Project", "square-plus", 1),
    NavRouteSpec("queue", "Processing Queue", "list-video", 2),
    NavRouteSpec("clips", "Generated Clips", "clapperboard", 3),
    NavRouteSpec("templates", "Caption Templates", "captions", 4),
)

SETTINGS_CHILD_ROUTES: tuple[NavRouteSpec, ...] = (
    NavRouteSpec("settings.general", "General", "settings", 9),
    NavRouteSpec("settings.appearance", "Appearance", "sun", 10),
    NavRouteSpec("settings.ai_providers", "AI Providers", "globe", 5),
    NavRouteSpec("settings.transcription", "Transcription", "captions", 6),
    NavRouteSpec("settings.performance", "Performance", "clock", 7),
    NavRouteSpec("settings.dependencies", "Dependencies", "download", 8),
)

ALL_ROUTES_BY_KEY: dict[str, NavRouteSpec] = {
    spec.key: spec for spec in (*PRIMARY_NAV_ROUTES, *SETTINGS_CHILD_ROUTES)
}
ALL_ROUTES_BY_INDEX: dict[int, NavRouteSpec] = {
    spec.page_index: spec for spec in (*PRIMARY_NAV_ROUTES, *SETTINGS_CHILD_ROUTES)
}

LEGACY_ROUTE_ALIASES: dict[str, str] = {
    "dashboard": "dashboard",
    "new project": "new_project",
    "processing queue": "queue",
    "generated clips": "clips",
    "caption templates": "templates",
    "settings": "settings.general",
    "general": "settings.general",
    "appearance": "settings.appearance",
    "ai providers": "settings.ai_providers",
    "ai provider settings": "settings.ai_providers",
    "transcription": "settings.transcription",
    "speech-to-text": "settings.transcription",
    "performance": "settings.performance",
    "dependencies": "settings.dependencies",
    "dependency manager": "settings.dependencies",
}


class _NavItemButton(QPushButton):
    """Floating sidebar row with fixed 30x30 icon container that turns solid red with white icon when active."""

    def __init__(
        self,
        label: str,
        icon_name: str,
        *,
        has_chevron: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("nav")
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(42)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._label_text = label
        self._icon_name = icon_name
        self._has_chevron = has_chevron
        self._chevron_expanded = False
        self._collapsed = False
        self._parent_active = False
        self.setAccessibleName(label)
        self.setToolTip(label)
        self.setText("")  # Painted crisply with aligned icon box and zero layout shift

    def set_collapsed(self, collapsed: bool) -> None:
        self._collapsed = bool(collapsed)
        self.setToolTip(self._label_text)
        self.update()

    def set_chevron_expanded(self, expanded: bool) -> None:
        self._chevron_expanded = bool(expanded)
        self.update()

    def set_parent_active(self, active: bool) -> None:
        self._parent_active = bool(active)
        self.setProperty("parentActive", "true" if active else "false")
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def is_visually_active(self) -> bool:
        return self.isChecked() or self._parent_active

    def paintEvent(self, _event) -> None:
        t = theme_manager().tokens
        active = self.is_visually_active()
        hovered = self.underMouse()

        painter = QPainter(self)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)

        # Draw crisp pill background natively so macOS QMacStyle never drops soft red tints
        bg_rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        if active:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(t["brand_tint"]))
            painter.drawRoundedRect(bg_rect, 10, 10)
        elif hovered:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(t["bg_nav_hover"]))
            painter.drawRoundedRect(bg_rect, 10, 10)

        if self.hasFocus():
            focus_pen = QPen(QColor(t["brand_border_soft"] if active else t["border_strong"]), 1.0)
            painter.setPen(focus_pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(bg_rect, 10, 10)

        box_size = 30
        icon_size = 17
        cy = self.height() / 2.0
        box_y = cy - box_size / 2.0

        if self._collapsed:
            box_x = (self.width() - box_size) / 2.0
        else:
            box_x = 8.0

        box_rect = QRectF(box_x, box_y, box_size, box_size)
        if active:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(t["brand_primary"]))
            painter.drawRoundedRect(box_rect, 8, 8)
            icon_color = "#FFFFFF"
        else:
            icon_color = t["text_primary"] if hovered else t["text_secondary"]

        icon_pm = lucide_icon(self._icon_name, icon_color, icon_size).pixmap(icon_size, icon_size)
        ix = box_x + (box_size - icon_size) / 2.0
        iy = box_y + (box_size - icon_size) / 2.0
        painter.drawPixmap(int(round(ix)), int(round(iy)), icon_pm)

        if not self._collapsed:
            right_reserve = 26 if self._has_chevron else 10
            text_x = int(round(box_x + box_size + 10))
            text_w = max(0, self.width() - text_x - right_reserve)
            font = QFont(self.font())
            font.setPixelSize(13)
            font.setWeight(QFont.Weight.DemiBold if active else QFont.Weight.Medium)
            painter.setFont(font)
            text_color = t["text_primary"] if (active or hovered) else t["text_secondary"]
            painter.setPen(QColor(text_color))
            elided = painter.fontMetrics().elidedText(
                self._label_text, Qt.TextElideMode.ElideRight, text_w,
            )
            painter.drawText(
                QRect(text_x, 0, text_w, self.height()),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                elided,
            )

            if self._has_chevron:
                chev_name = "chevron-up" if self._chevron_expanded else "chevron-down"
                chev_color = t["text_primary"] if active else t["text_secondary"]
                chev_pm = lucide_icon(chev_name, chev_color, 14).pixmap(14, 14)
                cx = self.width() - 22
                cy_i = int(round((self.height() - 14) / 2.0))
                painter.drawPixmap(cx, cy_i, chev_pm)

        painter.end()


class _SettingsChildButton(QPushButton):
    """Indented Settings child item with reliable cross-platform soft red pill highlight."""

    def __init__(self, label: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("navChild")
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(36)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._label_text = label
        self.setAccessibleName(f"Settings {label}")
        self.setToolTip(f"Settings › {label}")
        self.setText("")

    def paintEvent(self, _event) -> None:
        t = theme_manager().tokens
        checked = self.isChecked()
        hovered = self.underMouse()

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        bg_rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        if checked:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(t["brand_tint"]))
            painter.drawRoundedRect(bg_rect, 8, 8)
        elif hovered:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(t["bg_nav_hover"]))
            painter.drawRoundedRect(bg_rect, 8, 8)

        if self.hasFocus():
            painter.setPen(QPen(QColor(t["brand_border_soft"] if checked else t["border_strong"]), 1.0))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(bg_rect, 8, 8)

        font = QFont(self.font())
        font.setPixelSize(12)
        font.setWeight(QFont.Weight.DemiBold if checked else QFont.Weight.Medium)
        painter.setFont(font)
        color = t["brand_accent"] if checked else (t["text_primary"] if hovered else t["text_secondary"])
        painter.setPen(QColor(color))
        text_rect = QRect(12, 0, max(0, self.width() - 20), self.height())
        elided = painter.fontMetrics().elidedText(self._label_text, Qt.TextElideMode.ElideRight, text_rect.width())
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, elided)
        painter.end()


class _SettingsTreeContainer(QWidget):
    """Indented nested Settings container drawing a clean Suzuki-inspired vertical tree connector."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(32, 4, 0, 4)
        self._layout.setSpacing(3)
        self._buttons: list[QPushButton] = []

    def add_child_button(self, button: QPushButton) -> None:
        self._buttons.append(button)
        self._layout.addWidget(button)

    def paintEvent(self, _event) -> None:
        if not self._buttons:
            return
        t = theme_manager().tokens
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pen = QPen(QColor(t["border_subtle"]), 1.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)

        trunk_x = 22.0
        centers = [btn.y() + btn.height() / 2.0 for btn in self._buttons if not btn.isHidden()]
        if not centers:
            painter.end()
            return
        top_y = 2.0
        bottom_y = centers[-1]
        painter.drawLine(int(trunk_x), int(top_y), int(trunk_x), int(bottom_y))
        for cy in centers:
            painter.drawLine(int(trunk_x), int(cy), int(trunk_x + 7), int(cy))
        painter.end()


class FloatingSidebar(QFrame):
    """Premium inset floating navigation sidebar with collapse toggle and expandable Settings tree."""

    EXPANDED_WIDTH = 240
    COLLAPSED_WIDTH = 70

    route_selected = Signal(str)
    collapsed_changed = Signal(bool)
    settings_expanded_changed = Signal(bool)

    def __init__(
        self,
        *,
        collapsed: bool = False,
        settings_expanded: bool = False,
        last_settings_route: str = "settings.general",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("floatingSidebar")
        self._collapsed = bool(collapsed)
        self._settings_expanded = bool(settings_expanded)
        self._last_settings_route = (
            last_settings_route if last_settings_route in ALL_ROUTES_BY_KEY else "settings.general"
        )
        self._active_route = "dashboard"
        self._resolved_theme = theme_manager().resolved

        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.setFixedWidth(self.COLLAPSED_WIDTH if self._collapsed else self.EXPANDED_WIDTH)

        # Subtle floating elevation shadow (lightweight, non-animated)
        self._shadow = QGraphicsDropShadowEffect(self)
        self._shadow.setBlurRadius(18)
        self._shadow.setOffset(0, 3)
        self.setGraphicsEffect(self._shadow)

        self._root = QVBoxLayout(self)
        self._root.setContentsMargins(12, 14, 12, 14)
        self._root.setSpacing(8)

        # 1. Brand header (expanded full logo + top-right collapse button; collapsed symbol + expand toggle)
        self._header_widget = QWidget()
        self._header_widget.setStyleSheet("background: transparent;")
        self._header_layout = QVBoxLayout(self._header_widget)
        self._header_layout.setContentsMargins(0, 0, 0, 2)
        self._header_layout.setSpacing(6)

        self._header_row = QHBoxLayout()
        self._header_row.setContentsMargins(0, 0, 0, 0)
        self._header_row.setSpacing(6)

        self.brand_label = QLabel(objectName="brand")
        self.brand_label.setAccessibleName("AutoClip Studio")
        self.brand_label.setToolTip("AutoClip Studio")
        self.brand_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.collapse_btn = QPushButton(objectName="sidebarCollapseBtn")
        self.collapse_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.collapse_btn.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.collapse_btn.setIconSize(QSize(14, 14))
        self.collapse_btn.clicked.connect(self.toggle_collapsed)

        # Balancing left spacer keeps the official logo dead-center beside the top-right collapse button
        self._header_left_balance = QWidget()
        self._header_left_balance.setFixedSize(28, 1)
        self._header_left_balance.setStyleSheet("background: transparent;")

        self._header_row.addWidget(self._header_left_balance, 0)
        self._header_row.addWidget(self.brand_label, 1)
        self._header_row.addWidget(self.collapse_btn, 0, Qt.AlignmentFlag.AlignTop)
        self._header_layout.addLayout(self._header_row)

        self._collapsed_toggle_row = QHBoxLayout()
        self._collapsed_toggle_row.setContentsMargins(0, 0, 0, 0)
        self._collapsed_toggle_row.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.expand_btn = QPushButton(objectName="sidebarCollapseBtn")
        self.expand_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.expand_btn.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.expand_btn.setIconSize(QSize(14, 14))
        self.expand_btn.setToolTip("Expand sidebar")
        self.expand_btn.setAccessibleName("Expand sidebar")
        self.expand_btn.clicked.connect(self.toggle_collapsed)
        self._collapsed_toggle_row.addWidget(self.expand_btn)
        self._collapsed_toggle_host = QWidget()
        self._collapsed_toggle_host.setStyleSheet("background: transparent;")
        self._collapsed_toggle_host.setLayout(self._collapsed_toggle_row)
        self._header_layout.addWidget(self._collapsed_toggle_host)
        self._root.addWidget(self._header_widget)

        top_div = QFrame(objectName="divider")
        self._root.addWidget(top_div)

        # 2. Scrollable navigation body (prevents overflow when Settings expanded on compact windows)
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._nav_host = QWidget()
        self._nav_host.setStyleSheet("background: transparent;")
        self._nav_layout = QVBoxLayout(self._nav_host)
        self._nav_layout.setContentsMargins(0, 4, 0, 4)
        self._nav_layout.setSpacing(5)

        self.primary_buttons: dict[str, _NavItemButton] = {}
        self.settings_child_buttons: dict[str, QPushButton] = {}
        self._route_buttons_by_index: dict[int, QPushButton] = {}

        for spec in PRIMARY_NAV_ROUTES:
            btn = _NavItemButton(spec.label, spec.icon, parent=self._nav_host)
            btn.clicked.connect(lambda _c=False, k=spec.key: self._on_primary_clicked(k))
            self._nav_layout.addWidget(btn)
            self.primary_buttons[spec.key] = btn
            self._route_buttons_by_index[spec.page_index] = btn

        mid_div_wrap = QWidget()
        mid_div_wrap.setStyleSheet("background: transparent;")
        mid_div_lay = QVBoxLayout(mid_div_wrap)
        mid_div_lay.setContentsMargins(4, 6, 4, 6)
        mid_div_lay.addWidget(QFrame(objectName="divider"))
        self._nav_layout.addWidget(mid_div_wrap)

        # Expandable Settings parent + nested children
        self.settings_button = _NavItemButton("Settings", "settings", has_chevron=True, parent=self._nav_host)
        self.settings_button.clicked.connect(self._on_settings_parent_clicked)
        self._nav_layout.addWidget(self.settings_button)

        self.settings_tree = _SettingsTreeContainer(self._nav_host)
        for spec in SETTINGS_CHILD_ROUTES:
            child = _SettingsChildButton(spec.label, parent=self.settings_tree)
            child.clicked.connect(lambda _c=False, k=spec.key: self._on_settings_child_clicked(k))
            self.settings_tree.add_child_button(child)
            self.settings_child_buttons[spec.key] = child
            self._route_buttons_by_index[spec.page_index] = child
        self._nav_layout.addWidget(self.settings_tree)
        self._nav_layout.addStretch(1)

        self._scroll.setWidget(self._nav_host)
        self._root.addWidget(self._scroll, 1)

        # 3. Anchored compact version footer
        bot_div = QFrame(objectName="divider")
        self._root.addWidget(bot_div)
        self.version_label = QLabel(f"Version {__version__}", objectName="helperText")
        self.version_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._root.addWidget(self.version_label)

        self._sync_collapsed_visuals()
        self.apply_theme(self._resolved_theme)

    @property
    def collapsed(self) -> bool:
        return self._collapsed

    @property
    def settings_expanded(self) -> bool:
        return self._settings_expanded

    @property
    def last_settings_route(self) -> str:
        return self._last_settings_route

    def ordered_nav_buttons_by_page_index(self) -> list[QPushButton]:
        return [self._route_buttons_by_index[idx] for idx in sorted(self._route_buttons_by_index)]

    def set_collapsed(self, collapsed: bool, *, emit_signal: bool = True) -> None:
        target = bool(collapsed)
        if self._collapsed == target:
            return
        self._collapsed = target
        self._sync_collapsed_visuals()
        self._apply_brand_pixmap(self._resolved_theme)
        if emit_signal:
            self.collapsed_changed.emit(self._collapsed)

    def toggle_collapsed(self) -> None:
        self.set_collapsed(not self._collapsed, emit_signal=True)

    def set_settings_expanded(self, expanded: bool, *, emit_signal: bool = True) -> None:
        target = bool(expanded)
        self._settings_expanded = target
        self.settings_button.set_chevron_expanded(target)
        self.settings_tree.setVisible((not self._collapsed) and self._settings_expanded)
        if emit_signal:
            self.settings_expanded_changed.emit(self._settings_expanded)

    def set_active_route(self, route_key: str) -> None:
        if route_key not in ALL_ROUTES_BY_KEY:
            return
        self._active_route = route_key
        is_settings = route_key.startswith("settings.")
        if is_settings:
            self._last_settings_route = route_key
            if not self._collapsed and not self._settings_expanded:
                self._settings_expanded = True
                self.settings_button.set_chevron_expanded(True)
                self.settings_tree.setVisible(True)

        for key, btn in self.primary_buttons.items():
            btn.setChecked(key == route_key)
        for key, btn in self.settings_child_buttons.items():
            btn.setChecked(key == route_key)
        self.settings_button.setChecked(False)
        self.settings_button.set_parent_active(is_settings)

    def _on_primary_clicked(self, key: str) -> None:
        self.set_active_route(key)
        self.route_selected.emit(key)

    def _on_settings_parent_clicked(self) -> None:
        if self._collapsed:
            self._open_collapsed_settings_menu()
            return
        if not self._settings_expanded:
            self.set_settings_expanded(True, emit_signal=True)
            if not self._active_route.startswith("settings."):
                self.set_active_route(self._last_settings_route)
                self.route_selected.emit(self._last_settings_route)
        else:
            self.set_settings_expanded(False, emit_signal=True)
        # Keep checked visual purely driven by active child state
        self.settings_button.setChecked(False)
        self.settings_button.set_parent_active(self._active_route.startswith("settings."))

    def _on_settings_child_clicked(self, key: str) -> None:
        self.set_active_route(key)
        self.route_selected.emit(key)

    def _open_collapsed_settings_menu(self) -> None:
        """Compact native Qt submenu anchored right of the collapsed Settings icon."""
        t = theme_manager().tokens
        menu = QMenu(self)
        menu.setObjectName("settingsFlyoutMenu")
        for spec in SETTINGS_CHILD_ROUTES:
            is_cur = spec.key == self._active_route
            icon_color = t["brand_accent"] if is_cur else t["text_secondary"]
            act = menu.addAction(lucide_icon("check" if is_cur else spec.icon, icon_color, 14), spec.label)
            act.triggered.connect(lambda _c=False, k=spec.key: self._on_settings_child_clicked(k))
        anchor = self.settings_button.mapToGlobal(QPoint(self.settings_button.width() + 8, 0))
        self.settings_button.setChecked(False)
        menu.exec(anchor)
        self.settings_button.set_parent_active(self._active_route.startswith("settings."))

    def _sync_collapsed_visuals(self) -> None:
        self.setFixedWidth(self.COLLAPSED_WIDTH if self._collapsed else self.EXPANDED_WIDTH)
        if self._collapsed:
            self._root.setContentsMargins(10, 14, 10, 12)
        else:
            self._root.setContentsMargins(12, 14, 12, 14)

        self._header_left_balance.setVisible(not self._collapsed)
        self.collapse_btn.setVisible(not self._collapsed)
        self._collapsed_toggle_host.setVisible(self._collapsed)

        for btn in self.primary_buttons.values():
            btn.set_collapsed(self._collapsed)
        self.settings_button.set_collapsed(self._collapsed)
        self.settings_button.set_chevron_expanded(self._settings_expanded)
        self.settings_tree.setVisible((not self._collapsed) and self._settings_expanded)

        self.version_label.setText(f"v{__version__}" if self._collapsed else f"Version {__version__}")
        self.collapse_btn.setToolTip("Collapse sidebar")
        self.collapse_btn.setAccessibleName("Collapse sidebar")

    def apply_theme(self, resolved: str = "") -> None:
        self._resolved_theme = resolved or theme_manager().resolved
        t = theme_manager().tokens
        shadow_alpha = 20 if self._resolved_theme == "light" else 48
        self._shadow.setColor(QColor(15, 23, 42, shadow_alpha))
        self.collapse_btn.setIcon(lucide_icon("chevron-left", t["text_secondary"], 14))
        self.expand_btn.setIcon(lucide_icon("chevron-right", t["text_secondary"], 14))
        self._apply_brand_pixmap(self._resolved_theme)
        for btn in self.primary_buttons.values():
            btn.update()
        self.settings_button.update()
        for child in self.settings_child_buttons.values():
            child.update()
        self.settings_tree.update()

    def _apply_brand_pixmap(self, resolved_theme: str) -> None:
        dpr = max(1.0, self.devicePixelRatioF())
        if self._collapsed:
            sym_path = asset_path("symbol.png")
            image = QImage(str(sym_path)) if sym_path.is_file() else QImage()
            if not image.isNull():
                pixmap = QPixmap.fromImage(image)
                target = int(round(32 * dpr))
                scaled = pixmap.scaled(
                    target, target,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                scaled.setDevicePixelRatio(dpr)
                self.brand_label.setPixmap(scaled)
                self.brand_label.setFixedHeight(36)
                return
            self.brand_label.setText("AC")
            self.brand_label.setFixedHeight(36)
            return

        wordmark_path = asset_path("AutoClipTrans.png")
        image = QImage(str(wordmark_path)) if wordmark_path.is_file() else QImage()
        if not image.isNull():
            if resolved_theme == "light":
                # Keep the official red brand symbol untouched while rendering wordmark text crisp dark charcoal.
                image = image.convertToFormat(QImage.Format.Format_ARGB32)
                w, h = image.width(), image.height()
                for y in range(h):
                    for x in range(w):
                        c = image.pixelColor(x, y)
                        if (
                            c.alpha() > 20
                            and c.red() > 160
                            and c.green() > 155
                            and c.blue() > 155
                            and abs(c.red() - c.blue()) < 35
                        ):
                            image.setPixelColor(x, y, QColor(23, 24, 28, c.alpha()))
            pixmap = QPixmap.fromImage(image)
            scaled = pixmap.scaledToWidth(
                int(round(162 * dpr)),
                Qt.TransformationMode.SmoothTransformation,
            )
            scaled.setDevicePixelRatio(dpr)
            self.brand_label.setPixmap(scaled)
            self.brand_label.setFixedHeight(max(38, int(round(scaled.height() / dpr)) + 4))
        else:
            self.brand_label.setText("AutoClip Studio")
            self.brand_label.setFixedHeight(38)


class _ThemeMiniPreview(QWidget):
    """Crisp painted mini-app thumbnail illustrating Light, Dark, or System floating sidebar layout."""

    def __init__(self, mode_id: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._mode_id = mode_id
        self.setFixedHeight(86)
        self.setMinimumWidth(140)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        clip = QPainterPath()
        clip.addRoundedRect(rect, 8, 8)
        painter.setClipPath(clip)

        if self._mode_id == "system":
            half = rect.width() / 2.0
            self._paint_half(painter, rect, LIGHT_TOKENS, QRectF(rect.x(), rect.y(), half, rect.height()))
            self._paint_half(painter, rect, DARK_TOKENS, QRectF(rect.x() + half, rect.y(), half, rect.height()))
            painter.setClipPath(clip)
            painter.setPen(QPen(QColor("#8892A4"), 1.0))
            painter.drawLine(int(rect.x() + half), int(rect.y()), int(rect.x() + half), int(rect.bottom()))
        else:
            tok = LIGHT_TOKENS if self._mode_id == "light" else DARK_TOKENS
            self._paint_half(painter, rect, tok, rect)

        painter.setClipping(False)
        painter.setPen(QPen(QColor(theme_manager().tokens["border_subtle"]), 1.0))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(rect, 8, 8)
        painter.end()

    @staticmethod
    def _paint_half(painter: QPainter, full: QRectF, tok: dict[str, str], clip_rect: QRectF) -> None:
        painter.save()
        painter.setClipRect(clip_rect, Qt.ClipOperation.IntersectClip)
        painter.fillRect(full, QColor(tok["bg_app"]))
        # Floating mini sidebar
        sb = QRectF(full.x() + 7, full.y() + 7, 42, full.height() - 14)
        painter.setPen(QPen(QColor(tok["border_subtle"]), 0.8))
        painter.setBrush(QColor(tok["bg_sidebar"]))
        painter.drawRoundedRect(sb, 6, 6)
        # Active red icon + soft tint pill inside sidebar
        pill = QRectF(sb.x() + 5, sb.y() + 14, sb.width() - 10, 11)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(tok["brand_tint"]))
        painter.drawRoundedRect(pill, 3, 3)
        painter.setBrush(QColor(tok["brand_primary"]))
        painter.drawRoundedRect(QRectF(pill.x() + 2, pill.y() + 2, 7, 7), 2, 2)
        # Inactive nav rows
        painter.setBrush(QColor(tok["border_subtle"]))
        for row_y in (30, 42, 54):
            painter.drawRoundedRect(QRectF(sb.x() + 6, sb.y() + row_y, sb.width() - 14, 5), 2, 2)

        # Main content cards
        cx = sb.right() + 8
        cw = full.right() - cx - 8
        painter.setBrush(QColor(tok["text_primary"]))
        painter.drawRoundedRect(QRectF(cx, full.y() + 10, min(52.0, cw * 0.45), 6), 3, 3)
        card1 = QRectF(cx, full.y() + 22, cw, 24)
        painter.setPen(QPen(QColor(tok["border_subtle"]), 0.8))
        painter.setBrush(QColor(tok["bg_surface"]))
        painter.drawRoundedRect(card1, 5, 5)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(tok["brand_primary"]))
        painter.drawRoundedRect(QRectF(card1.x() + 7, card1.y() + 8, 28, 8), 3, 3)
        card2 = QRectF(cx, full.y() + 52, cw, full.bottom() - (full.y() + 59))
        painter.setPen(QPen(QColor(tok["border_subtle"]), 0.8))
        painter.setBrush(QColor(tok["bg_surface"]))
        painter.drawRoundedRect(card2, 5, 5)
        painter.restore()


class ThemePreviewCard(QPushButton):
    """Selectable visual theme preview card for Settings > Appearance."""

    def __init__(self, mode_id: str, title: str, subtitle: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.mode_id = mode_id
        self.setObjectName("themePreviewCard")
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(166)
        self.setAccessibleName(f"{title} theme")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(10)

        self.preview = _ThemeMiniPreview(mode_id, self)
        self.preview.setFixedHeight(92)
        lay.addWidget(self.preview)

        foot = QHBoxLayout()
        foot.setContentsMargins(2, 0, 2, 0)
        foot.setSpacing(8)
        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(2)
        self.title_lbl = QLabel(title)
        self.title_lbl.setStyleSheet("font-size: 13px; font-weight: 600; background: transparent;")
        self.sub_lbl = QLabel(subtitle, objectName="muted")
        text_col.addWidget(self.title_lbl)
        text_col.addWidget(self.sub_lbl)
        foot.addLayout(text_col, 1)

        self.check_badge = QLabel()
        self.check_badge.setFixedSize(20, 20)
        self.check_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        foot.addWidget(self.check_badge, 0, Qt.AlignmentFlag.AlignVCenter)
        lay.addLayout(foot)

        self.toggled.connect(self._sync_badge)
        self._sync_badge(self.isChecked())

    def sizeHint(self) -> QSize:
        return QSize(210, 166)

    def _sync_badge(self, checked: bool) -> None:
        t = theme_manager().tokens
        if checked:
            self.check_badge.setStyleSheet(
                f"background: {t['brand_primary']}; border-radius: 10px; border: none;"
            )
            self.check_badge.setPixmap(lucide_icon("check", "#FFFFFF", 12).pixmap(12, 12))
        else:
            self.check_badge.setStyleSheet(
                f"background: transparent; border: 1.5px solid {t['border_strong']}; border-radius: 10px;"
            )
            self.check_badge.setPixmap(QPixmap())

    def apply_theme(self, _resolved: str = "") -> None:
        self._sync_badge(self.isChecked())
        self.preview.update()
