import os
from dataclasses import asdict
from datetime import datetime
from threading import Event
from time import monotonic

import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QTextCursor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFileDialog, QPushButton

from autoclip_studio.database import Database
from autoclip_studio.config import SettingsStore
from autoclip_studio.gui import MainWindow
from autoclip_studio.log_view import LEVEL_COLORS, LogView
from autoclip_studio.models import ProjectOptions
from autoclip_studio.providers import AIProvider
from tests.test_provider_reliability import response


@pytest.fixture(scope="module")
def app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QApplication.instance() or QApplication([])


def test_log_selection_shortcuts_colors_and_context_menu(app):
    view = LogView()
    view.resize(540, 220)
    view.show()
    view.add_entry("INFO", "alpha beta gamma")
    view.add_entry("SUCCESS", "second line")
    view.add_entry("WARNING", "third line")
    view.add_entry("ERROR", "fourth line")
    view.flush()
    app.processEvents()
    assert view.isReadOnly()
    original = view.log_text()
    datetime.fromisoformat(original[1:original.index("]")])

    cursor = view.textCursor()
    cursor.setPosition(original.index("alpha") + 2)
    view.setTextCursor(cursor)
    QTest.mouseDClick(view.viewport(), Qt.MouseButton.LeftButton, pos=view.cursorRect().center())
    assert view.textCursor().selectedText() == "alpha"
    QTest.keyClick(view, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    assert app.clipboard().text() == "alpha"
    menu = view.createStandardContextMenu()
    actions = {action.text().split("\t")[0].replace("&", "").lower(): action for action in menu.actions()}
    assert {"copy", "select all"} <= actions.keys()
    actions["select all"].trigger()
    actions["copy"].trigger()
    assert app.clipboard().text() == original
    QTest.keyClick(view, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    assert view.textCursor().selectionEnd() == len(original)

    cursor.setPosition(original.index("alpha"))
    view.setTextCursor(cursor)
    start = view.cursorRect(cursor).center()
    cursor.setPosition(original.index("second line") + len("second line"))
    end = view.cursorRect(cursor).center()
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(view.viewport(), pos=end)
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=end)
    assert "alpha beta gamma" in view.textCursor().selectedText()
    assert "second line" in view.textCursor().selectedText()

    for index, level in enumerate(LEVEL_COLORS):
        cursor = QTextCursor(view.document().findBlockByNumber(index))
        cursor.movePosition(QTextCursor.MoveOperation.Right)
        assert cursor.charFormat().foreground().color().name() == LEVEL_COLORS[level]
    view.close()


def test_log_does_not_scroll_away_from_selection_or_scrolled_up_view(app):
    view = LogView()
    view.resize(350, 100)
    view.show()
    for index in range(30):
        view.add_entry("INFO", f"line {index}")
    view.flush()
    app.processEvents()
    bar = view.verticalScrollBar()
    assert bar.value() == bar.maximum() > 0
    bar.setValue(2)
    view.add_entry("WARNING", "new line")
    view.flush()
    assert bar.value() == 2

    view.selectAll()
    bar.setValue(bar.maximum())
    old_position = bar.value()
    selected = view.textCursor().selectedText()
    view.add_entry("ERROR", "new error")
    view.flush()
    assert bar.value() == old_position < bar.maximum()
    assert view.textCursor().selectedText() == selected
    view.close()


def test_log_buttons_copy_clear_and_export_complete_redacted_log(app, monkeypatch, tmp_path):
    monkeypatch.setattr("autoclip_studio.gui.Database", lambda: Database(tmp_path / "test.db"))
    monkeypatch.setattr("autoclip_studio.config.SettingsStore.api_key", lambda *_args: "")
    window = MainWindow()
    window.show_page(2)
    buttons = {button.text(): button for button in window.findChildren(QPushButton)}
    error = "Authorization: Bearer secret-header\n" + "\n".join(f"detail {i}" for i in range(750))
    window.live_logs.add_entry("ERROR", error)
    buttons["Copy Logs"].click()
    full = app.clipboard().text()
    assert "detail 0\n" in full and full.endswith("detail 749")
    assert "secret-header" not in full
    path = tmp_path / "logs"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_args: (str(path), "Text files (*.txt)"))
    buttons["Export Logs"].click()
    assert path.with_suffix(".txt").read_text(encoding="utf-8") == full
    window.live_logs.add_entry("INFO", "pending entry")
    buttons["Clear Logs"].click()
    assert window.live_logs.log_text() == ""
    window.close()


def test_provider_settings_persist_and_background_retry_keeps_gui_responsive(app, monkeypatch, tmp_path):
    settings = SettingsStore(tmp_path / "settings.json")
    monkeypatch.setattr(settings, "api_key", lambda *_args: "")
    monkeypatch.setattr(settings, "set_api_key", lambda *_args: None)
    monkeypatch.setattr("autoclip_studio.gui.SettingsStore", lambda: settings)
    monkeypatch.setattr("autoclip_studio.gui.Database", lambda: Database(tmp_path / "test.db"))
    window = MainWindow()
    window.show_page(5)
    window.settings_provider.setCurrentText("custom")
    window.auto_fallback_check.setChecked(True)
    window.fallback_models_input.setText(" model-a, model-b, model-a ")
    window.save_provider()
    saved = SettingsStore(tmp_path / "settings.json").provider("custom")
    assert saved["auto_fallback"] and saved["fallback_models"] == ["model-a", "model-b"]
    window.settings_provider.setCurrentText("gemini")
    assert not window.auto_fallback_check.isChecked()
    window.settings_provider.setCurrentText("custom")
    assert window.auto_fallback_check.isChecked()
    assert window.fallback_models_input.text() == "model-a, model-b"

    requested = Event()
    ticks = []
    starts = []
    provider = AIProvider("custom", "private-value", "https://provider.invalid", "primary",
                          cancel_event=window.utility_cancel, notify=None)

    def post(*_args, **_kwargs):
        starts.append(True)
        requested.set()
        return response(429, "Slow down", **{"Retry-After": "30"})

    monkeypatch.setattr("autoclip_studio.providers.requests.post", post)
    try:
        window._run_utility(provider, provider.test, lambda _result: None)
        assert requested.wait(2)
        QTimer.singleShot(0, lambda: ticks.append("GUI processed timer while provider waits"))
        deadline = monotonic() + 2
        while monotonic() < deadline:
            app.processEvents()
            if "Retrying in 30 seconds" in window.live_logs.log_text():
                break
            Event().wait(0.01)
        assert ticks and window.utility_threads[0].isRunning()
        assert "Retrying in 30 seconds" in window.live_logs.log_text()
    finally:
        window.utility_cancel.set()
        if window.utility_threads:
            window.utility_threads[0].wait(3000)
        app.processEvents()
        window.close()
    assert starts == [True]


def test_retry_button_restores_saved_project_options(app, monkeypatch, tmp_path):
    db = Database(tmp_path / "test.db")
    monkeypatch.setattr("autoclip_studio.gui.Database", lambda: db)
    monkeypatch.setattr("autoclip_studio.config.SettingsStore.api_key", lambda *_args: "")
    options = ProjectOptions("https://www.youtube.com/watch?v=example", clip_count=2, model="project-model")
    project_id = db.create_project(options.source, asdict(options))
    db.update_project(project_id, status="analysis_failed", progress=40, error="Quota exhausted")
    window = MainWindow()
    dispatched = []
    monkeypatch.setattr(window, "_start_worker", lambda *args, **kwargs: dispatched.append((args, kwargs)))
    window.show_page(2)
    assert not window.retry_analysis_button.isEnabled()
    window.queue_table.selectRow(0)
    assert window.retry_analysis_button.isEnabled()
    window.retry_analysis_button.click()
    assert dispatched == [((project_id, options), {"resume_analysis": True})]
    window.close()
