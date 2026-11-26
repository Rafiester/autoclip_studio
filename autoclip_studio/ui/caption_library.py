from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QRectF, QSignalBlocker, Qt, QThread, QUrl, Signal, Slot
from PySide6.QtGui import QColor, QFont, QImage, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtMultimedia import QMediaPlayer, QVideoFrame, QVideoSink
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QProgressBar, QPushButton,
    QScrollArea, QSizePolicy, QSlider, QSpinBox, QVBoxLayout, QWidget,
)

from ..caption_preview import PREVIEW_DURATION, render_preview
from ..captions import (
    CAPTION_Y_MAX,
    CAPTION_Y_MIN,
    DEFAULT_CAPTION_Y_RATIO,
    PLATFORM_SAFE_AREAS,
    TEMPLATE_DEFAULT,
    TEMPLATES,
    CaptionTemplate,
    FaceZone,
    estimate_caption_bbox,
    resolve_caption_y_center,
    template_font,
)
from ..diagnostics import log_error
from ..process import CancelledError, ProcessManager


CATEGORIES = ("All", "Minimal", "Viral", "Animated", "Cinematic")


class _PreviewRender(QThread):
    ready = Signal(str, object)
    failed = Signal(str)

    def __init__(self, name: str, parent: QWidget, *, y_ratio: float = DEFAULT_CAPTION_Y_RATIO) -> None:
        super().__init__(parent)
        self.name = name
        self.y_ratio = y_ratio
        self.manager = ProcessManager()

    def run(self) -> None:
        try:
            self.ready.emit(self.name, render_preview(self.name, self.manager, y_ratio=self.y_ratio))
        except CancelledError:
            pass
        except Exception as error:
            log_error("Caption preview generation failed", error)
            self.failed.emit(self.name)


class _Canvas(QWidget):
    def __init__(self, template: CaptionTemplate) -> None:
        super().__init__()
        self.template = template
        self.image = QImage()
        self.setFixedHeight(100)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)

    def show_frame(self, image: QImage) -> None:
        self.image = image
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        bounds = QRectF(self.rect())
        clip = QPainterPath()
        clip.addRoundedRect(bounds, 9, 9)
        painter.setClipPath(clip)
        painter.fillRect(bounds, QColor(self.template.background))
        if not self.image.isNull():
            frame = self.image.size().scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
            target = QRectF((self.width() - frame.width()) / 2, (self.height() - frame.height()) / 2,
                            frame.width(), frame.height())
            painter.drawImage(target, self.image)
            return

        font = QFont(template_font(self.template))
        font.setBold(self.template.bold)
        font.setPixelSize(21)
        painter.setFont(font)
        while painter.fontMetrics().horizontalAdvance(self.template.sample) > self.width() - 16 and font.pixelSize() > 10:
            font.setPixelSize(font.pixelSize() - 1)
            painter.setFont(font)
        metrics = painter.fontMetrics()
        width = metrics.horizontalAdvance(self.template.sample)
        baseline = bounds.center().y() + (metrics.ascent() - metrics.descent()) / 2
        sample = QPainterPath()
        x = (self.width() - width) / 2
        sample.addText(x, baseline, font, self.template.sample)
        effect = self.template.effect
        fill = QColor(self.template.primary)
        if effect in {"box", "podcast"}:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(self.template.accent))
            painter.drawRoundedRect(sample.boundingRect().adjusted(-7, -5, 7, 5), 4, 4)
        if effect == "glow":
            glow = QColor(self.template.accent)
            glow.setAlpha(110)
            painter.strokePath(sample, QPen(glow, 6))
        elif effect == "gradient":
            fill = QLinearGradient(x, 0, x + width, 0)
            fill.setColorAt(0, QColor(self.template.primary))
            fill.setColorAt(1, QColor(self.template.accent))
        painter.strokePath(sample, QPen(QColor("#101116"), 2.5, Qt.PenStyle.SolidLine,
                                        Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        painter.fillPath(sample, fill)
        if effect in {"emphasis", "viral"}:
            end = self.template.sample.index(" ")
            active = QPainterPath()
            active.addText(x, baseline, font, self.template.sample[:end])
            painter.fillPath(active, QColor(self.template.accent))


def _flag(widget: QWidget, property_name: str, value: bool) -> None:
    if widget.property(property_name) != value:
        widget.setProperty(property_name, value)
        widget.style().unpolish(widget)
        widget.style().polish(widget)
        widget.update()


class _Card(QFrame):
    def __init__(self, name: str, template: CaptionTemplate, select: Callable[[str], None],
                 toggle: Callable[[str], None], replay: Callable[[str], None]) -> None:
        super().__init__(objectName="captionCard")
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)
        self.canvas = _Canvas(template)
        self.progress = QProgressBar(objectName="captionPreviewProgress")
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(3)
        self.progress.setRange(0, round(PREVIEW_DURATION * 1000))
        self.progress.setValue(0)
        self.progress.setAccessibleName(f"{name} preview progress")
        title = QLabel(name, objectName="captionTitle")
        description = QLabel(template.description, objectName="captionDescription", wordWrap=True)
        description.setMinimumHeight(40)
        description.setAlignment(Qt.AlignmentFlag.AlignTop)
        description.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.MinimumExpanding)
        self.play = QPushButton("Play Preview", objectName="captionPreviewPlay")
        self.play.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.replay = QPushButton("\u21bb", objectName="captionPreviewReplay")
        self.replay.setMinimumWidth(36)
        self.replay.setAccessibleName(f"Replay {name} preview")
        self.replay.setToolTip("Replay preview")
        self.select = QPushButton("Use Template", objectName="captionSelect")
        self.select.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.play.clicked.connect(lambda: toggle(name))
        self.replay.clicked.connect(lambda: replay(name))
        self.select.clicked.connect(lambda: select(name))
        controls = QHBoxLayout()
        controls.setSpacing(5)
        controls.addWidget(self.play, 1)
        controls.addWidget(self.replay)
        layout.addWidget(self.canvas)
        layout.addWidget(self.progress)
        layout.addWidget(title)
        layout.addWidget(description)
        layout.addLayout(controls)
        layout.addWidget(self.select)
        self.name = name
        self.preview_state("Play Preview")

    def preview_state(self, label: str, *, loading: bool = False, replay: bool = False) -> None:
        self.play.setText(label)
        self.play.setEnabled(not loading)
        self.play.setAccessibleName(f"{label} for {self.name}")
        self.replay.setEnabled(replay)
        _flag(self, "playing", label == "Pause")

    def set_selected(self, selected: bool) -> None:
        _flag(self, "selected", selected)
        _flag(self.select, "selected", selected)
        self.select.setText("\u2713 Selected" if selected else "Use Template")
        self.select.setAccessibleName(f"{self.name}, selected as default" if selected else f"Use {self.name} as default")


_MOCK_SPEAKER_FACE = (0.34, 0.22, 0.66, 0.49)


class _PositionPreview(QWidget):
    """Interactive 9:16 portrait preview showing caption placement, mock face zone, and platform guides."""

    y_picked = Signal(float)

    def __init__(self, template_name: str = TEMPLATE_DEFAULT) -> None:
        super().__init__()
        self.template_name = template_name if template_name in TEMPLATES else TEMPLATE_DEFAULT
        self.y_ratio = DEFAULT_CAPTION_Y_RATIO
        self.avoid_faces = True
        self.show_safe_area = True
        self.safe_platform = "all"
        self.resolved_y = DEFAULT_CAPTION_Y_RATIO
        self.shifted_by_face = False
        self.setFixedSize(180, 320)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setAccessibleName("Live 9:16 caption positioning preview")
        self.setToolTip("Click or drag vertically to adjust caption height.")
        self._recompute()

    def set_state(
        self,
        *,
        template_name: str | None = None,
        y_ratio: float | None = None,
        avoid_faces: bool | None = None,
        show_safe_area: bool | None = None,
        safe_platform: str | None = None,
    ) -> tuple[float, bool]:
        if template_name is not None and template_name in TEMPLATES:
            self.template_name = template_name
        if y_ratio is not None:
            self.y_ratio = max(CAPTION_Y_MIN, min(CAPTION_Y_MAX, float(y_ratio)))
        if avoid_faces is not None:
            self.avoid_faces = bool(avoid_faces)
        if show_safe_area is not None:
            self.show_safe_area = bool(show_safe_area)
        if safe_platform is not None and safe_platform in PLATFORM_SAFE_AREAS:
            self.safe_platform = safe_platform
        self._recompute()
        self.update()
        return self.resolved_y, self.shifted_by_face

    def _recompute(self) -> None:
        template = TEMPLATES[self.template_name]
        mock_zones = (FaceZone(0.0, 5.0, (_MOCK_SPEAKER_FACE,)),)
        self.resolved_y, self.shifted_by_face = resolve_caption_y_center(
            0.0, 2.0, template.sample, template, 1080, 1920,
            y_ratio=self.y_ratio, avoid_faces=self.avoid_faces, face_regions=mock_zones,
        )

    def _emit_from_y(self, py: float) -> None:
        ratio = max(CAPTION_Y_MIN, min(CAPTION_Y_MAX, py / max(1, self.height())))
        self.y_picked.emit(round(ratio, 2))

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            self._emit_from_y(event.position().y())

    def mouseMoveEvent(self, event) -> None:
        if event.buttons() & Qt.MouseButton.LeftButton:
            self._emit_from_y(event.position().y())

    def mouseReleaseEvent(self, event) -> None:
        self.setCursor(Qt.CursorShape.OpenHandCursor)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        w, h = float(self.width()), float(self.height())
        bounds = QRectF(0.5, 0.5, w - 1.0, h - 1.0)
        clip = QPainterPath()
        clip.addRoundedRect(bounds, 10, 10)
        painter.setClipPath(clip)

        # Studio backdrop gradient
        bg = QLinearGradient(0, 0, 0, h)
        bg.setColorAt(0.0, QColor("#181B24"))
        bg.setColorAt(1.0, QColor("#101218"))
        painter.fillRect(bounds, bg)

        # Stylized reference speaker silhouette (upper chest & head for clear vertical height gauge)
        silhouette = QColor("#272C3B")
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(silhouette)
        # Head & neck
        head_x, head_y = w * _MOCK_SPEAKER_FACE[0], h * _MOCK_SPEAKER_FACE[1]
        head_w = w * (_MOCK_SPEAKER_FACE[2] - _MOCK_SPEAKER_FACE[0])
        head_h = h * (_MOCK_SPEAKER_FACE[3] - _MOCK_SPEAKER_FACE[1]) * 0.90
        painter.drawEllipse(QRectF(head_x + head_w * 0.08, head_y, head_w * 0.84, head_h))
        # Upper torso / lower chest region
        torso_path = QPainterPath()
        torso_path.moveTo(w * 0.16, h * 0.92)
        torso_path.quadTo(w * 0.18, h * 0.56, w * 0.38, h * 0.50)
        torso_path.lineTo(w * 0.62, h * 0.50)
        torso_path.quadTo(w * 0.82, h * 0.56, w * 0.84, h * 0.92)
        torso_path.closeSubpath()
        painter.drawPath(torso_path)

        # Mock face boundary indicator
        face_rect = QRectF(
            w * _MOCK_SPEAKER_FACE[0],
            h * _MOCK_SPEAKER_FACE[1],
            w * (_MOCK_SPEAKER_FACE[2] - _MOCK_SPEAKER_FACE[0]),
            h * (_MOCK_SPEAKER_FACE[3] - _MOCK_SPEAKER_FACE[1]),
        )
        face_pen = QPen(QColor("#5EC2FF" if self.avoid_faces else "#7D8599"), 1.2, Qt.PenStyle.DashLine)
        painter.setPen(face_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(face_rect, 6, 6)
        painter.setFont(QFont("Segoe UI", 7))
        painter.drawText(face_rect.adjusted(2, -13, 0, 0), Qt.AlignmentFlag.AlignLeft, "Face Zone")

        # Conservative Platform Safe-Area UI Guides (never exported)
        if self.show_safe_area:
            safe = PLATFORM_SAFE_AREAS.get(self.safe_platform, PLATFORM_SAFE_AREAS["all"])
            danger_fill = QColor(229, 57, 53, 44)
            danger_pen = QPen(QColor(255, 90, 79, 140), 1.0, Qt.PenStyle.DashLine)
            top_h = h * safe.top
            bot_top = h * (1.0 - safe.bottom)
            right_w = w * safe.right
            right_top = h * safe.right_rail_top
            painter.fillRect(QRectF(0, 0, w, top_h), danger_fill)
            painter.fillRect(QRectF(0, bot_top, w, h - bot_top), danger_fill)
            painter.fillRect(QRectF(w - right_w, right_top, right_w, max(0.0, bot_top - right_top)), danger_fill)

            safe_box = QRectF(
                w * safe.left,
                top_h,
                w * (1.0 - safe.left - safe.right),
                max(20.0, bot_top - top_h),
            )
            painter.setPen(danger_pen)
            painter.drawRect(safe_box)

            guide_font = QFont("Segoe UI", 7)
            painter.setFont(guide_font)
            painter.setPen(QColor("#FFB4AE"))
            painter.drawText(QRectF(6, 2, w - 12, top_h - 2), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, f"{safe.label} · Top UI")
            painter.drawText(QRectF(6, bot_top + 3, w - 12, 16), Qt.AlignmentFlag.AlignLeft, "Platform UI Zone")

        # User requested Y hairline
        pref_py = h * self.y_ratio
        if abs(self.resolved_y - self.y_ratio) > 0.01:
            painter.setPen(QPen(QColor(255, 255, 255, 70), 1.0, Qt.PenStyle.DotLine))
            painter.drawLine(8, int(round(pref_py)), int(w - 8), int(round(pref_py)))

        # Rendered sample caption centered at resolved_y
        template = TEMPLATES[self.template_name]
        box_w_norm, box_h_norm = estimate_caption_bbox(template.sample, template, 1080, 1920)
        cap_w, cap_h = w * box_w_norm, h * box_h_norm
        cap_cy = h * self.resolved_y

        font = QFont(template_font(template))
        font.setBold(template.bold)
        px_size = max(10, round(template.size * (w / 1080.0)))
        font.setPixelSize(px_size)
        painter.setFont(font)
        text = template.sample.upper() if template.uppercase else template.sample
        while painter.fontMetrics().horizontalAdvance(text) > w * 0.84 and px_size > 8:
            px_size -= 1
            font.setPixelSize(px_size)
            painter.setFont(font)

        fm = painter.fontMetrics()
        tw = fm.horizontalAdvance(text)
        tx = w * 0.08 if (template.alignment in (1, 4, 7)) else (w - tw) * 0.5
        baseline = cap_cy + (fm.ascent() - fm.descent()) * 0.5
        path = QPainterPath()
        path.addText(tx, baseline, font, text)

        if template.effect in {"box", "podcast"}:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(template.accent))
            painter.drawRoundedRect(path.boundingRect().adjusted(-5, -3, 5, 3), 3, 3)
        elif template.effect == "glow":
            glow = QColor(template.accent)
            glow.setAlpha(110)
            painter.strokePath(path, QPen(glow, 4))

        fill = QColor(template.primary)
        if template.effect == "gradient":
            grad = QLinearGradient(tx, 0, tx + tw, 0)
            grad.setColorAt(0, QColor(template.primary))
            grad.setColorAt(1, QColor(template.accent))
            fill = grad
        painter.strokePath(path, QPen(QColor("#101116"), 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        painter.fillPath(path, fill)

        # Subtle caption box guide outline around resolved position
        guide_rect = QRectF((w - cap_w) * 0.5 if template.alignment not in (1, 4, 7) else w * 0.065, cap_cy - cap_h * 0.5, cap_w, cap_h)
        painter.setPen(QPen(QColor("#5FE3B4" if not self.shifted_by_face else "#FFD45A"), 1.0, Qt.PenStyle.SolidLine))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(guide_rect, 4, 4)

        # Crisp outer border
        painter.setClipping(False)
        painter.setPen(QPen(QColor(255, 255, 255, 28), 1.0))
        painter.drawRoundedRect(bounds, 10, 10)


class CaptionLibrary(QWidget):
    idle = Signal()

    def __init__(
        self,
        selected: str,
        select: Callable[[str], None],
        create: Callable[[], None],
        *,
        sync_mode: str = "precise",
        offset_ms: int = 0,
        on_timing_changed: Callable[[str, int], None] | None = None,
        y_ratio: float = DEFAULT_CAPTION_Y_RATIO,
        avoid_faces: bool = True,
        show_safe_area: bool = True,
        safe_platform: str = "all",
        on_positioning_changed: Callable[[float, bool, bool, str], None] | None = None,
    ) -> None:
        super().__init__()
        self.active: str | None = None
        self.render: _PreviewRender | None = None
        self.columns = 0
        self.visible_cards: list[str] = []
        self._selected_template = selected if selected in TEMPLATES else TEMPLATE_DEFAULT
        self._on_timing_changed = on_timing_changed
        self._on_positioning_changed = on_positioning_changed
        self.player = QMediaPlayer(self)
        self.sink = QVideoSink(self)
        self.player.setVideoSink(self.sink)
        self.sink.videoFrameChanged.connect(self._frame)
        self.player.positionChanged.connect(self._position)
        self.player.playbackStateChanged.connect(self._playback_changed)
        self.player.mediaStatusChanged.connect(self._playback_changed)
        self.player.errorOccurred.connect(self._playback_error)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        self.search = QLineEdit(placeholderText="Search templates…", clearButtonEnabled=True)
        self.search.setAccessibleName("Search caption templates")
        self.category = QComboBox()
        self.category.setAccessibleName("Caption template category")
        self.category.addItems(CATEGORIES)

        self.sync_mode = QComboBox()
        self.sync_mode.setAccessibleName("Caption timing mode")
        self.sync_mode.addItem("Timing: Precise", "precise")
        self.sync_mode.addItem("Timing: Readable", "readable")
        self.sync_mode.setToolTip(
            "Precise locks captions directly to spoken onsets.\n"
            "Readable adds subtle lead-in and extended hold in silences."
        )

        self.offset_spin = QSpinBox()
        self.offset_spin.setRange(-500, 500)
        self.offset_spin.setSingleStep(20)
        self.offset_spin.setSuffix(" ms")
        self.offset_spin.setMinimumWidth(90)
        self.offset_spin.setAccessibleName("Caption timing offset milliseconds")
        self.offset_spin.setToolTip("Fine-tune caption timing offset (-500 ms earlier to +500 ms later).")
        self.set_timing_preferences(sync_mode, offset_ms)
        self.sync_mode.currentIndexChanged.connect(self._emit_timing)
        self.offset_spin.valueChanged.connect(self._emit_timing)

        create_button = QPushButton("Create Project", objectName="primary")
        create_button.clicked.connect(create)
        toolbar.addWidget(self.search, 1)
        toolbar.addWidget(self.category)
        toolbar.addWidget(self.sync_mode)
        toolbar.addWidget(self.offset_spin)
        toolbar.addWidget(create_button)
        layout.addLayout(toolbar)

        self.notice = QLabel(objectName="captionDescription", wordWrap=True)
        self.notice.hide()
        layout.addWidget(self.notice)

        workspace = QHBoxLayout()
        workspace.setSpacing(14)
        self.scroll = QScrollArea()
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.scroll.viewport().installEventFilter(self)
        self.body = QWidget()
        self.grid = QGridLayout(self.body)
        self.grid.setSpacing(10)
        self.grid.setContentsMargins(0, 0, 5, 0)
        self.grid.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.scroll.setWidget(self.body)
        workspace.addWidget(self.scroll, 1)

        # Right-hand interactive Caption Positioning & Platform Safe Area panel
        pos_card = QFrame(objectName="sectionCard")
        pos_card.setFixedWidth(284)
        pos_layout = QVBoxLayout(pos_card)
        pos_layout.setContentsMargins(14, 14, 14, 14)
        pos_layout.setSpacing(10)

        pos_title = QLabel("Caption Positioning", objectName="captionTitle")
        pos_sub = QLabel(
            "Default 73% anchors lower chest below speaker faces.",
            objectName="captionDescription",
            wordWrap=True,
        )
        pos_layout.addWidget(pos_title)
        pos_layout.addWidget(pos_sub)

        self.pos_preview = _PositionPreview(self._selected_template)
        self.pos_preview.y_picked.connect(self._on_preview_y_picked)
        pos_layout.addWidget(self.pos_preview, 0, Qt.AlignmentFlag.AlignHCenter)

        self.pos_status_label = QLabel(objectName="captionDescription", wordWrap=True)
        self.pos_status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pos_layout.addWidget(self.pos_status_label)

        slider_header = QHBoxLayout()
        slider_header.setSpacing(8)
        slider_lbl = QLabel("Vertical Position")
        self.y_value_label = QLabel(objectName="transferMetric")
        self.y_value_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        slider_header.addWidget(slider_lbl)
        slider_header.addStretch(1)
        slider_header.addWidget(self.y_value_label)
        pos_layout.addLayout(slider_header)

        self.y_slider = QSlider(Qt.Orientation.Horizontal)
        self.y_slider.setRange(round(CAPTION_Y_MIN * 100), round(CAPTION_Y_MAX * 100))
        self.y_slider.setSingleStep(1)
        self.y_slider.setPageStep(5)
        self.y_slider.setAccessibleName("Caption vertical position slider")
        pos_layout.addWidget(self.y_slider)

        self.avoid_faces_check = QCheckBox("Avoid Covering Faces")
        self.avoid_faces_check.setToolTip(
            "Uses detected face tracking zones to keep subtitles clear of visible speakers."
        )
        self.safe_area_check = QCheckBox("Show Platform Safe Area")
        self.safe_area_check.setToolTip(
            "Overlays conservative platform UI bounds in preview only (never exported)."
        )
        self.safe_platform_combo = QComboBox()
        self.safe_platform_combo.setAccessibleName("Platform safe area preset")
        for key, safe in PLATFORM_SAFE_AREAS.items():
            self.safe_platform_combo.addItem(f"Guide: {safe.label}", key)

        pos_layout.addWidget(self.avoid_faces_check)
        pos_layout.addWidget(self.safe_area_check)
        pos_layout.addWidget(self.safe_platform_combo)

        self.reset_position_btn = QPushButton("Reset to Default")
        self.reset_position_btn.setAccessibleName("Reset caption position to default")
        pos_layout.addWidget(self.reset_position_btn)
        pos_layout.addStretch(1)

        self.set_positioning_preferences(y_ratio, avoid_faces, show_safe_area, safe_platform)
        self.y_slider.valueChanged.connect(self._on_positioning_widget_changed)
        self.avoid_faces_check.toggled.connect(self._on_positioning_widget_changed)
        self.safe_area_check.toggled.connect(self._on_positioning_widget_changed)
        self.safe_platform_combo.currentIndexChanged.connect(self._on_positioning_widget_changed)
        self.reset_position_btn.clicked.connect(self.reset_positioning_defaults)

        workspace.addWidget(pos_card, 0)
        layout.addLayout(workspace, 1)

        self.cards = {name: _Card(name, template, select, self._toggle, self._replay)
                      for name, template in TEMPLATES.items()}
        for card in self.cards.values():
            card.setParent(self.body)
        self.search.textChanged.connect(self._filter)
        self.category.currentTextChanged.connect(self._filter)
        self.set_selected(selected)
        self._filter()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.scroll.viewport() and event.type() == QEvent.Type.Resize and hasattr(self, "cards"):
            self._layout_cards()
        return super().eventFilter(watched, event)

    def show_notice(self, text: str) -> None:
        self.notice.setText(text)
        self.notice.setVisible(bool(text))

    def set_selected(self, name: str) -> None:
        if name in TEMPLATES:
            self._selected_template = name
        for candidate, card in self.cards.items():
            card.set_selected(candidate == name)
        if hasattr(self, "pos_preview"):
            self._sync_preview()

    def set_positioning_preferences(
        self,
        y_ratio: float,
        avoid_faces: bool,
        show_safe_area: bool,
        platform: str,
    ) -> None:
        clean_y = max(CAPTION_Y_MIN, min(CAPTION_Y_MAX, float(y_ratio)))
        pct = round(clean_y * 100)
        clean_platform = platform.strip().lower() if isinstance(platform, str) and platform.strip().lower() in PLATFORM_SAFE_AREAS else "all"
        idx = self.safe_platform_combo.findData(clean_platform)
        with QSignalBlocker(self.y_slider), QSignalBlocker(self.avoid_faces_check), QSignalBlocker(self.safe_area_check), QSignalBlocker(self.safe_platform_combo):
            self.y_slider.setValue(pct)
            self.avoid_faces_check.setChecked(bool(avoid_faces))
            self.safe_area_check.setChecked(bool(show_safe_area))
            self.safe_platform_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.safe_platform_combo.setEnabled(bool(show_safe_area))
        self._sync_preview()

    def positioning_preferences(self) -> tuple[float, bool, bool, str]:
        return (
            round(self.y_slider.value() / 100.0, 2),
            self.avoid_faces_check.isChecked(),
            self.safe_area_check.isChecked(),
            str(self.safe_platform_combo.currentData() or "all"),
        )

    def reset_positioning_defaults(self) -> None:
        self.set_positioning_preferences(DEFAULT_CAPTION_Y_RATIO, True, True, "all")
        self._on_positioning_widget_changed()

    def _on_preview_y_picked(self, ratio: float) -> None:
        self.y_slider.setValue(round(ratio * 100))

    def _sync_preview(self) -> None:
        y_ratio, avoid_faces, show_safe, platform = self.positioning_preferences()
        resolved_y, shifted = self.pos_preview.set_state(
            template_name=self._selected_template,
            y_ratio=y_ratio,
            avoid_faces=avoid_faces,
            show_safe_area=show_safe,
            safe_platform=platform,
        )
        self.y_value_label.setText(f"{round(y_ratio * 100)}%")
        if shifted:
            self.pos_status_label.setText(f"Auto-adjusted to {round(resolved_y * 100)}% to clear face")
        elif avoid_faces:
            self.pos_status_label.setText(f"{self._selected_template} · {round(resolved_y * 100)}% · Face-safe")
        else:
            self.pos_status_label.setText(f"{self._selected_template} · {round(resolved_y * 100)}% · Manual Y")

    def _on_positioning_widget_changed(self) -> None:
        y_ratio, avoid_faces, show_safe, platform = self.positioning_preferences()
        self.safe_platform_combo.setEnabled(show_safe)
        self._sync_preview()
        if self._on_positioning_changed is not None:
            self._on_positioning_changed(y_ratio, avoid_faces, show_safe, platform)

    def set_timing_preferences(self, sync_mode: str, offset_ms: int) -> None:
        clean_mode = sync_mode.strip().lower() if isinstance(sync_mode, str) else "precise"
        idx = self.sync_mode.findData(clean_mode)
        with QSignalBlocker(self.sync_mode), QSignalBlocker(self.offset_spin):
            self.sync_mode.setCurrentIndex(idx if idx >= 0 else 0)
            self.offset_spin.setValue(max(-500, min(500, int(offset_ms))))

    def timing_preferences(self) -> tuple[str, int]:
        return str(self.sync_mode.currentData() or "precise"), int(self.offset_spin.value())

    def _emit_timing(self) -> None:
        if self._on_timing_changed is not None:
            mode, offset_ms = self.timing_preferences()
            self._on_timing_changed(mode, offset_ms)

    def _filter(self) -> None:
        category = self.category.currentText()
        terms = self.search.text().casefold().split()
        self.visible_cards = [
            name for name, template in TEMPLATES.items()
            if (category == "All" or template.category == category)
            and all(term in f"{name} {template.description} {template.category}".casefold() for term in terms)
        ]
        if self.active and self.active not in self.visible_cards:
            self.stop()
        self.show_notice("" if self.visible_cards else "No matching templates. Try a different search or category.")
        self.scroll.verticalScrollBar().setValue(0)
        self._layout_cards(force=True)

    def _layout_cards(self, *, force: bool = False) -> None:
        width = self.scroll.viewport().width()
        columns = 3 if width >= 720 else (2 if width >= 460 else 1)
        if columns == self.columns and not force:
            return
        self.columns = columns
        for index in range(4):
            self.grid.setColumnStretch(index, int(index < columns))
        for name, card in self.cards.items():
            self.grid.removeWidget(card)
            card.setVisible(name in self.visible_cards)
        for index, name in enumerate(self.visible_cards):
            self.grid.addWidget(self.cards[name], index // columns, index % columns)

    @property
    def busy(self) -> bool:
        return self.render is not None

    def stop(self) -> None:
        previous, self.active = self.active, None
        if previous:
            self.player.stop()
            self.player.setSource(QUrl())
            card = self.cards[previous]
            card.preview_state("Play Preview")
            card.progress.setValue(0)
            card.canvas.show_frame(QImage())
        if self.render:
            self.render.manager.cancel()
        self.show_notice("")

    def _toggle(self, name: str) -> None:
        if self.active == name:
            if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
                self.player.pause()
            elif self.player.mediaStatus() == QMediaPlayer.MediaStatus.EndOfMedia:
                self._replay(name)
            elif not self.player.source().isEmpty():
                self.player.play()
            return
        self.stop()
        self.active = name
        self.cards[name].preview_state("Preparing…", loading=True)
        if not self.render:
            self._start_render(name)

    def _start_render(self, name: str) -> None:
        y_ratio = self.positioning_preferences()[0] if hasattr(self, "y_slider") else DEFAULT_CAPTION_Y_RATIO
        self.render = _PreviewRender(name, self, y_ratio=y_ratio)
        self.render.ready.connect(self._ready)
        self.render.failed.connect(self._failed)
        self.render.finished.connect(self._render_finished)
        self.render.start()

    @Slot(str, object)
    def _ready(self, name: str, path: Path) -> None:
        if self.active == name and self.render and not self.render.manager.cancel_event.is_set():
            self.player.setSource(QUrl.fromLocalFile(str(path)))
            self.player.play()

    @Slot(str)
    def _failed(self, name: str) -> None:
        if self.active == name and self.render and not self.render.manager.cancel_event.is_set():
            self.stop()
            self.show_notice(f"Could not prepare the {name} preview. Select Play Preview to try again.")

    @Slot()
    def _render_finished(self) -> None:
        if self.render:
            self.render.deleteLater()
        self.render = None
        if self.active and self.player.source().isEmpty():
            self._start_render(self.active)
        else:
            self.idle.emit()

    def _replay(self, name: str) -> None:
        if self.active == name and not self.player.source().isEmpty():
            self.player.setPosition(0)
            self.player.play()

    def _frame(self, frame: QVideoFrame) -> None:
        if self.active and self.player.mediaStatus() != QMediaPlayer.MediaStatus.EndOfMedia and frame.isValid():
            self.cards[self.active].canvas.show_frame(frame.toImage())

    def _position(self, position: int) -> None:
        if self.active:
            self.cards[self.active].progress.setValue(position)

    def _playback_changed(self) -> None:
        if not self.active or self.player.source().isEmpty():
            return
        card = self.cards[self.active]
        if self.player.mediaStatus() == QMediaPlayer.MediaStatus.EndOfMedia:
            card.preview_state("Replay", replay=True)
            card.canvas.show_frame(QImage())
        elif self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            card.preview_state("Pause", replay=True)
        elif self.player.playbackState() == QMediaPlayer.PlaybackState.PausedState:
            card.preview_state("Play", replay=True)

    def _playback_error(self, _error: QMediaPlayer.Error, _message: str) -> None:
        if self.active:
            name = self.active
            self.stop()
            self.show_notice(f"Could not play the {name} preview. Select Play Preview to try again.")
