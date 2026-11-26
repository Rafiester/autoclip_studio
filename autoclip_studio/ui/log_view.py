from __future__ import annotations

from collections import deque
from datetime import datetime

from PySide6.QtCore import QTimer
from PySide6.QtGui import QColor, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import QApplication, QPlainTextEdit

from ..diagnostics import redact


LEVEL_COLORS = {
    "INFO": "#cfdbed",
    "SUCCESS": "#7de5a7",
    "WARNING": "#ffd58a",
    "ERROR": "#ff9caa",
}

MAX_LOG_ENTRIES = 4000


class LogView(QPlainTextEdit):
    MAX_LOG_ENTRIES = MAX_LOG_ENTRIES

    def __init__(self) -> None:
        super().__init__()
        self.setReadOnly(True)
        self.setUndoRedoEnabled(False)
        self.setMaximumBlockCount(MAX_LOG_ENTRIES)
        self.setAccessibleName("Processing logs")
        self._entries: deque[tuple[str, str]] = deque(maxlen=MAX_LOG_ENTRIES)
        self._pending: list[tuple[str, str]] = []
        self._level_filter: str = "ALL"
        self._search_filter: str = ""
        self.auto_scroll: bool = True
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self.flush)

    def add_entry(self, level: str, message: str) -> None:
        timestamp = datetime.now().astimezone().isoformat(sep=" ", timespec="seconds")
        line = f"[{timestamp}] [{level}] {redact(message)}"
        self._entries.append((level, line))
        self._pending.append((level, line))
        if not self._timer.isActive():
            self._timer.start()

    def _matches_filter(self, level: str, line: str) -> bool:
        if self._level_filter != "ALL" and level != self._level_filter:
            return False
        if self._search_filter and self._search_filter not in line.lower():
            return False
        return True

    def set_filter(self, *, level: str | None = None, search: str | None = None) -> None:
        if level is not None:
            self._level_filter = level.strip().upper() or "ALL"
        if search is not None:
            self._search_filter = search.strip().lower()
        self.flush()
        super().clear()
        cursor = QTextCursor(self.document())
        cursor.beginEditBlock()
        for lvl, line in self._entries:
            if not self._matches_filter(lvl, line):
                continue
            style = QTextCharFormat()
            style.setForeground(QColor(LEVEL_COLORS.get(lvl, LEVEL_COLORS["INFO"])))
            if cursor.position():
                cursor.insertBlock()
            cursor.insertText(line, style)
        cursor.endEditBlock()
        if self.auto_scroll:
            bar = self.verticalScrollBar()
            bar.setValue(bar.maximum())

    def flush(self) -> None:
        self._timer.stop()
        if not self._pending:
            return
        bar = self.verticalScrollBar()
        position = bar.value()
        current = self.textCursor()
        selection = (current.anchor(), current.position()) if current.hasSelection() else None
        follow = self.auto_scroll and position >= bar.maximum() and selection is None
        cursor = QTextCursor(self.document())
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.beginEditBlock()
        for level, message in self._pending:
            if not self._matches_filter(level, message):
                continue
            style = QTextCharFormat()
            style.setForeground(QColor(LEVEL_COLORS.get(level, LEVEL_COLORS["INFO"])))
            if cursor.position():
                cursor.insertBlock()
            cursor.insertText(message, style)
        cursor.endEditBlock()
        self._pending.clear()
        if selection is not None:
            current.setPosition(selection[0])
            current.setPosition(selection[1], QTextCursor.MoveMode.KeepAnchor)
            self.setTextCursor(current)
        bar.setValue(bar.maximum() if follow else position)

    def log_text(self) -> str:
        self.flush()
        return "\n".join(line for _lvl, line in self._entries)

    def copy_all(self) -> None:
        QApplication.clipboard().setText(self.log_text())

    def clear(self) -> None:
        self._timer.stop()
        self._pending.clear()
        self._entries.clear()
        super().clear()
