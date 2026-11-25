from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QTimer
from PySide6.QtGui import QColor, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import QApplication, QPlainTextEdit

from .diagnostics import redact


LEVEL_COLORS = {
    "INFO": "#cfdbed",
    "SUCCESS": "#7de5a7",
    "WARNING": "#ffd58a",
    "ERROR": "#ff9caa",
}


class LogView(QPlainTextEdit):
    def __init__(self) -> None:
        super().__init__()
        self.setReadOnly(True)
        self.setUndoRedoEnabled(False)
        self.setAccessibleName("Processing logs")
        self._pending: list[tuple[str, str]] = []
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self.flush)

    def add_entry(self, level: str, message: str) -> None:
        timestamp = datetime.now().astimezone().isoformat(sep=" ", timespec="seconds")
        self._pending.append((level, f"[{timestamp}] [{level}] {redact(message)}"))
        if not self._timer.isActive():
            self._timer.start()

    def flush(self) -> None:
        self._timer.stop()
        if not self._pending:
            return
        bar = self.verticalScrollBar()
        position = bar.value()
        current = self.textCursor()
        selection = (current.anchor(), current.position()) if current.hasSelection() else None
        follow = position >= bar.maximum() and selection is None
        cursor = QTextCursor(self.document())
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.beginEditBlock()
        for level, message in self._pending:
            style = QTextCharFormat()
            style.setForeground(QColor(LEVEL_COLORS[level]))
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
        return self.toPlainText()

    def copy_all(self) -> None:
        QApplication.clipboard().setText(self.log_text())

    def clear(self) -> None:
        self._timer.stop()
        self._pending.clear()
        super().clear()
