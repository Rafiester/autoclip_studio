from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from autoclip_studio import dependencies as deps
from autoclip_studio import media
from autoclip_studio.process import ProcessManager
from autoclip_studio.ui.queue_workspace import ErrorDetailsDialog
from scripts import setup as setup_script


def _app() -> QApplication:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QApplication.instance() or QApplication([])


def test_setup_preserves_newer_yt_dlp_and_never_downgrades() -> None:
    installed = {
        "pyside6": "6.8.3",
        "faster-whisper": "1.2.1",
        "av": "15.1.0",
        "yt-dlp": "2027.01.15",  # Newer than requirements.txt floor!
        "opencv-python-headless": "4.10.0.84",
        "requests": "2.32.5",
        "keyring": "25.6.0",
        "platformdirs": "4.3.8",
        "pywin32": "312",
        "mlx-whisper": "0.4.2",
        "psutil": "7.2.2",
        "nvidia-ml-py": "13.615.71",
    }
    problems, to_install = setup_script.missing_or_outdated_requirements(installed_map=installed)
    assert problems == []
    assert to_install == []


def test_setup_flags_only_missing_or_outdated_packages_selectively() -> None:
    installed = {
        "pyside6": "6.8.3",
        "faster-whisper": "1.2.1",
        "av": "15.1.0",
        "yt-dlp": "2025.11.12",  # Below >=2026.11.23 floor
        "opencv-python-headless": "4.10.0.84",
        "requests": "2.32.5",
        "keyring": "25.6.0",
        "platformdirs": "4.3.8",
        "pywin32": "312",
        "mlx-whisper": "0.4.2",
        "psutil": "7.2.2",
        "nvidia-ml-py": "13.615.71",
    }
    problems, to_install = setup_script.missing_or_outdated_requirements(installed_map=installed)
    assert len(problems) == 1
    assert "yt-dlp" in problems[0]
    assert to_install == ["yt-dlp>=2026.11.23"]


def test_package_manager_detection_supports_uv_without_pip_and_pip_without_uv(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "app"
    tools = root / "tools"
    tools.mkdir(parents=True)
    uv_bin = tools / ("uv.exe" if os.name == "nt" else "uv")
    uv_bin.write_text("", encoding="utf-8")
    fake_py = root / ".venv" / ("Scripts" if os.name == "nt" else "bin") / ("python.exe" if os.name == "nt" else "python3")
    fake_py.parent.mkdir(parents=True)
    fake_py.write_text("", encoding="utf-8")

    # Case 1: uv-managed venv WITHOUT pip
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: subprocess.CompletedProcess([], 1, "", "No module named pip"))
    pm_uv = deps.detect_package_manager(root=root, python_exe=fake_py)
    assert pm_uv.kind == "uv"
    assert pm_uv.has_uv and not pm_uv.has_pip

    # Case 2: pip-managed venv WITHOUT uv
    uv_bin.unlink()
    monkeypatch.setattr("shutil.which", lambda _name: None)
    monkeypatch.setattr(deps, "uv_executable_path", lambda _r: root / "missing_uv")
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: subprocess.CompletedProcess([], 0, "pip 26.3", ""))
    orig_is_file = Path.is_file

    def _hide_system_uv(self: Path) -> bool:
        if self.name == "uv":
            return False
        return orig_is_file(self)

    monkeypatch.setattr(Path, "is_file", _hide_system_uv)
    pm_pip = deps.detect_package_manager(root=root, python_exe=fake_py)
    assert pm_pip.kind == "pip"
    assert pm_pip.has_pip and not pm_pip.has_uv


def test_yt_dlp_update_blocks_active_job_and_rolls_back_on_broken_validation(tmp_path: Path, monkeypatch) -> None:
    with pytest.raises(RuntimeError, match="processing job is active"):
        deps.update_yt_dlp(is_job_active=lambda: True)

    calls: list[list[str]] = []
    validations = iter([(True, "2026.11.23"), (False, "ImportError: broken extractor")])
    monkeypatch.setattr(deps, "validate_yt_dlp", lambda _py=None: next(validations))

    def fake_install(specs, **_kw):
        calls.append(list(specs))
        return subprocess.CompletedProcess(specs, 0, "ok", "")

    monkeypatch.setattr(deps, "_run_package_install", fake_install)
    with pytest.raises(RuntimeError, match="Rolled back to yt-dlp 2026.11.23"):
        deps.update_yt_dlp(is_job_active=lambda: False, state_path=tmp_path / "dep_state.json")
    assert calls == [["yt-dlp"], ["yt-dlp==2026.11.23"]]


@pytest.mark.parametrize(
    ("stderr", "expected_cat", "expected_retryable"),
    [
        ("ERROR: unable to download video data: HTTP Error 403: Forbidden", "http_403", True),
        ("ERROR: HTTP Error 429: Too Many Requests", "http_429", True),
        ("ERROR: Sign in to confirm you're not a bot. Use --cookies", "auth_required", False),
        ("ERROR: No supported JavaScript runtime could be found; js challenge failed", "js_runtime_error", False),
        ("ERROR: Requested format is not available.", "format_unavailable", True),
        ("ERROR: ffprobe and ffmpeg not found. Please install one.", "missing_media_tool", False),
        ("ERROR: Postprocessing: Error merging formats", "merge_error", True),
        ("ERROR: [Errno 28] No space left on device", "disk_space", False),
        ("ERROR: [Errno 13] Permission denied: '/output/x.mp4'", "permission_error", False),
        ("ERROR: urlopen error [Errno 8] nodename nor servname provided", "network_error", True),
        ("ERROR: Unable to extract video data; signature extraction failed", "extractor_error", False),
        ("ERROR: Unexpected custom failure reason Authorization: Bearer sk-12345678901234567890123", "unknown", False),
    ],
)
def test_classify_download_error_categories_and_redaction(stderr: str, expected_cat: str, expected_retryable: bool) -> None:
    err = media.classify_download_error(stderr, exit_code=1)
    assert err.category == expected_cat
    assert err.retryable is expected_retryable
    assert "sk-12345678901234567890123" not in str(err)
    assert "returned non-zero exit status" not in err.summary.lower()


def test_yt_dlp_command_includes_detected_js_runtime(monkeypatch, tmp_path: Path) -> None:
    fake_node = tmp_path / "node"
    fake_node.write_text("", encoding="utf-8")
    monkeypatch.setattr(media, "bundled_binary", lambda _name: None)
    monkeypatch.setattr(media, "detect_js_runtime", lambda: ("node", str(fake_node)))
    cmd = media.yt_dlp_command()
    assert "--js-runtimes" in cmd
    assert f"node:{fake_node}" in cmd


def test_download_bounded_retry_and_fallback_format(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(media, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(media, "find_media_tool", lambda name: str(tmp_path / f"{name}.fake"))
    monkeypatch.setattr(media, "bundled_binary", lambda _name: None)
    monkeypatch.setattr("time.sleep", lambda _s: None)
    manager = ProcessManager()
    seen_formats: list[str] = []

    def fake_run(command, **_kw):
        fmt = command[command.index("-f") + 1]
        seen_formats.append(fmt)
        if len(seen_formats) < 3:
            raise subprocess.CalledProcessError(1, command, output="ERROR: HTTP Error 403: Forbidden")
        out = Path(command[command.index("-o") + 1].replace("%(ext)s", "mp4"))
        receipt = Path(command[command.index("--print-to-file") + 2])
        out.write_bytes(b"valid-bytes")
        receipt.write_text(str(out) + "\n", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(manager, "run", fake_run)
    path = media.acquire_source("https://www.youtube.com/watch?v=dSq0Z5XpoLc", True, manager)
    assert path.is_file()
    assert len(seen_formats) == 3
    assert seen_formats[-1] == media.FALLBACK_FORMAT


def test_error_details_dialog_expand_copy_and_actions() -> None:
    app = _app()
    dlg = ErrorDetailsDialog(
        "Processing failed",
        "YouTube rejected download (HTTP 403).\n\nTechnical details:\nCookie: secret123\nERROR: HTTP Error 403",
        allow_retry=True,
        allow_dependencies=True,
    )
    try:
        dlg.show()
        app.processEvents()
        assert not dlg.details_view.isVisible()
        assert "secret123" not in dlg.details_view.toPlainText()
        dlg._toggle_details()
        app.processEvents()
        assert dlg.details_view.isVisible()
        dlg._copy()
        assert "HTTP 403" in app.clipboard().text()
    finally:
        dlg.close()
        dlg.deleteLater()
