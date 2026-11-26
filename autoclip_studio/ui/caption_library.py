from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QRectF, Qt, QThread, QUrl, Signal, Slot
from PySide6.QtGui import QColor, QFont, QImage, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtMultimedia import QMediaPlayer, QVideoFrame, QVideoSink
from PySide6.QtWidgets import (
    QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QProgressBar, QPushButton,
    QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from ..caption_preview import PREVIEW_DURATION, render_preview
from ..captions import TEMPLATES, CaptionTemplate, template_font
from ..diagnostics import log_error
from ..process import CancelledError, ProcessManager


CATEGORIES = ("All", "Minimal", "Viral", "Animated", "Cinematic")


class _PreviewRender(QThread):
    ready = Signal(str, object)
    failed = Signal(str)

    def __init__(self, name: str, parent: QWidget) -> None:
        super().__init__(parent)
        self.name = name
        self.manager = ProcessManager()

    def run(self) -> None:
        try:
            self.ready.emit(self.name, render_preview(self.name, self.manager))
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


class CaptionLibrary(QWidget):
    idle = Signal()

    def __init__(self, selected: str, select: Callable[[str], None], create: Callable[[], None]) -> None:
        super().__init__()
        self.active: str | None = None
        self.render: _PreviewRender | None = None
        self.columns = 0
        self.visible_cards: list[str] = []
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
        self.search = QLineEdit(placeholderText="Search templates…", clearButtonEnabled=True)
        self.search.setAccessibleName("Search caption templates")
        self.category = QComboBox()
        self.category.setAccessibleName("Caption template category")
        self.category.addItems(CATEGORIES)
        create_button = QPushButton("Create Project", objectName="primary")
        create_button.clicked.connect(create)
        toolbar.addWidget(self.search, 1)
        toolbar.addWidget(self.category)
        toolbar.addWidget(create_button)
        layout.addLayout(toolbar)

        self.notice = QLabel(objectName="captionDescription", wordWrap=True)
        self.notice.hide()
        layout.addWidget(self.notice)
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
        layout.addWidget(self.scroll, 1)
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
        for candidate, card in self.cards.items():
            card.set_selected(candidate == name)

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
        columns = 4 if self.scroll.viewport().width() >= 860 else 3
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
        self.render = _PreviewRender(name, self)
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
