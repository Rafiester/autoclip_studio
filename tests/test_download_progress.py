import os

import pytest
from PySide6.QtGui import QFontMetrics
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from autoclip_studio.database import Database
from autoclip_studio.download_view import format_bytes, format_time
from autoclip_studio.gui import MainWindow
from autoclip_studio.media import DownloadProgress, _log_transfer


@pytest.fixture(scope="module")
def app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QApplication.instance() or QApplication([])


def test_uses_decimal_units_and_readable_eta():
    assert format_bytes(1_000, rate=True) == "1.00 KB/s"
    assert format_bytes(999_000, rate=True) == "999.00 KB/s"
    assert format_bytes(1_000_000, rate=True) == "1.00 MB/s"
    assert format_bytes(1_048_576, rate=True) == "1.05 MB/s"
    assert format_bytes(450_250_000) == "450.25 MB"
    assert format_bytes(2_350_000_000) == "2.35 GB"
    assert format_bytes(None) == "—"
    assert format_time(159.1) == "2 min 40 sec"
    assert format_time(3600) == "1 hr 0 min"
    assert format_time(None) == "—"


def test_downloader_hook_fields_and_raw_fragment_activity():
    logs, samples = [], []
    _log_transfer(
        "AUTOCLIP_TRANSFER\tdownloading\t450250000\tNA\t2350000000\t1030000.0\t160\tav01\tnone"
        "\t19.2% of ~2.19GiB at 1005.86KiB/s ETA 02:40 (frag 2/10)",
        logs.append, samples.append,
    )
    _log_transfer(
        "AUTOCLIP_TRANSFER\tfinished\tNA\t2350000000\tNA\tNA\tNA\tav01\tnone\t100% of 2.19GiB",
        logs.append, samples.append,
    )
    _log_transfer(
        'AUTOCLIP_TRANSFER\tdownloading\t1000\tNA\tnull\t"NaN"\t-1\tnone\topus\t1000B downloaded',
        logs.append, samples.append,
    )
    _log_transfer("[Merger] Merging formats", logs.append, samples.append)
    assert samples == [
        DownloadProgress(450_250_000, 2_350_000_000, True, 1_030_000, 160, "video", False),
        DownloadProgress(2_350_000_000, 2_350_000_000, False, None, None, "video", True),
        DownloadProgress(1000, None, False, None, None, "audio", False),
    ]
    assert logs[0].endswith("(frag 2/10)")
    assert logs[-1] == "[Merger] Merging formats"
    assert all("AUTOCLIP_TRANSFER" not in line for line in logs)


def test_download_card_metrics_activity_and_cancellation(app, monkeypatch, tmp_path):
    monkeypatch.setattr("autoclip_studio.gui.Database", lambda: Database(tmp_path / "projects.db"))
    monkeypatch.setattr("autoclip_studio.config.SettingsStore.api_key", lambda *_args: "")
    window = MainWindow()
    window.resize(760, 560)
    window.show()
    window.show_page(2)
    window.show_download(True)
    app.processEvents()
    view = window.download_view
    assert view.isVisible() and not window.processing_progress_row.isVisible()
    assert not view.cancel_button.icon().isNull()
    assert window.stack.currentWidget().minimumSizeHint().width() <= window.stack.currentWidget().width()
    metrics = QFontMetrics(view.percent.font())
    assert metrics.horizontalAdvance("111.1%") == metrics.horizontalAdvance("888.8%")
    window.on_download_progress(DownloadProgress(450_250_000, 2_350_000_000, False, 1_030_000, 160, "video", False))
    assert view.status.text() == "Downloading video"
    assert view.percent.text() == "19.2%"
    assert view.summary.text() == "450.25 MB of 2.35 GB"
    assert [label.text() for label in view.metric_values] == ["1.03 MB/s", "450.25 MB", "2.35 GB", "2 min 40 sec"]
    window.on_download_progress(DownloadProgress(500_000_000, 2_350_000_000, True, 1_030_000, 160, "video", False))
    QTest.qWait(250)
    assert view.bar.value() == 213
    assert view.metric_values[2].text() == "≈ 2.35 GB"
    window.on_download_progress(DownloadProgress(2_350_000_000, 2_350_000_000, False, None, None, "video", True))
    assert view.status.text() == "Video downloaded"
    assert view.percent.text() == "100.0%"
    window.on_download_progress(DownloadProgress(1_000_000, None, False, 90_000, None, "audio", False))
    assert view.status.text() == "Downloading audio"
    assert view.percent.text() == "—"
    assert view.summary.text() == "1.00 MB downloaded"
    assert view.bar.value() == 0
    window.on_download_log("[download] transferred fragment 3/11")
    assert "fragment 3/11" in window.live_logs.log_text()
    assert "fragment" not in view.status.text()

    window.worker = object()
    view.cancel_button.click()
    assert window.pipeline.manager.cancel_event.is_set()
    assert not view.cancel_button.isEnabled()
    assert view.status.text() == "Cancelling download…"
    window.on_download_progress(DownloadProgress(2_000_000, None, False, None, None, "audio", False))
    assert view.status.text() == "Cancelling download…"
    window.on_failed("test-project", "Processing cancelled")
    assert not view.isVisible() and window.processing_progress_row.isVisible()

    window.worker = None
    window.pipeline.manager.reset()
    window.show_download(True)
    window.on_progress(12, "Generating timestamped transcript")
    assert not view.isVisible()
    assert window.progress_text.text() == "12%"
    window.close()
