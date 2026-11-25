from __future__ import annotations

import hashlib
import http.server
import os
import socketserver
import threading
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from autoclip_studio import dependencies as deps
from autoclip_studio.speech_engine import SpeechSettings


def _app() -> QApplication:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QApplication.instance() or QApplication([])


def test_manifest_contains_core_components_and_default_model() -> None:
    specs, models, default_model = deps.load_manifest()
    assert default_model == "large-v3-turbo"
    ids = {s.id for s in specs}
    assert {
        "uv", "python_runtime", "python_venv", "pyside6", "python_packages",
        "ffmpeg", "yt_dlp", "opencv_face_model", "whisper_engine",
        "cuda_runtime", "whisper_model_large_v3_turbo", "caption_fonts",
    } <= ids
    assert any(m["id"] == "large-v3-turbo" for m in models)


def test_clean_vs_configured_and_cloud_only_and_cuda_inspection(tmp_path: Path, monkeypatch) -> None:
    # Clean missing APP_ROOT
    fake_root = tmp_path / "clean_checkout"
    fake_root.mkdir()
    monkeypatch.setattr(deps, "APP_ROOT", fake_root)
    monkeypatch.setattr(deps, "MODEL_CACHE", tmp_path / "cache" / "models")
    monkeypatch.setattr(deps, "gpu_info", lambda: None)
    monkeypatch.setattr(deps, "python_cuda", lambda: False)
    monkeypatch.setattr(deps, "subtitle_edit_engine", lambda: None)
    monkeypatch.setattr(deps, "find_executable", lambda *_a: None)
    monkeypatch.setattr(deps, "find_model", lambda *_a: None)
    monkeypatch.setattr("shutil.which", lambda _name: None)

    clean_statuses = deps.inspect_all_dependencies(cloud_only=True)
    by_id = {s.spec.id: s for s in clean_statuses}
    assert by_id["uv"].state == "Missing"
    assert by_id["python_venv"].state == "Missing"
    assert by_id["ffmpeg"].state == "Missing"
    assert by_id["cuda_runtime"].state == "Optional"
    assert by_id["whisper_model_large_v3_turbo"].state == "Optional"
    assert "cloud-only" in by_id["whisper_model_large_v3_turbo"].detail.lower()


def test_resumable_download_partial_recovery_and_sha256_corruption(tmp_path: Path, monkeypatch) -> None:
    payload = b"AutoClipStudio-BinaryPayload-" * 4096
    digest = hashlib.sha256(payload).hexdigest()

    class RangeHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            rng = self.headers.get("Range")
            if rng and rng.startswith("bytes="):
                start = int(rng.split("=")[1].split("-")[0])
                body = payload[start:]
                self.send_response(206)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Range", f"bytes {start}-{len(payload) - 1}/{len(payload)}")
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        def log_message(self, *_args):
            return

    server = socketserver.TCPServer(("127.0.0.1", 0), RangeHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        target = tmp_path / "pkg.bin"
        part = target.with_suffix(".bin.part")
        # Simulate interrupted partial download (first 1024 bytes already saved)
        part.write_bytes(payload[:1024])
        progress_events: list[deps.DownloadMetrics] = []
        deps.download_resumable(
            f"http://127.0.0.1:{port}/pkg.bin", target,
            expected_sha256=digest, on_progress=progress_events.append,
        )
        assert target.read_bytes() == payload
        assert not part.exists()
        assert progress_events

        # Corrupted SHA-256 check fails & removes bad partial file
        bad_target = tmp_path / "bad.bin"
        with pytest.raises(RuntimeError, match="SHA-256 verification failed"):
            deps.download_resumable(f"http://127.0.0.1:{port}/pkg.bin", bad_target, expected_sha256="0" * 64)
        assert not bad_target.exists()

        # Insufficient disk space check fails before download
        monkeypatch.setattr(deps, "check_disk_space", lambda _need, _p=None: (False, 100))
        with pytest.raises(RuntimeError, match="Insufficient disk space"):
            deps.download_resumable(f"http://127.0.0.1:{port}/pkg.bin", tmp_path / "nospace.bin")
    finally:
        server.shutdown()
        server.server_close()


def test_whisper_model_external_protection_and_owned_removal(tmp_path: Path, monkeypatch) -> None:
    own_cache = tmp_path / "own_models"
    ext_cache = tmp_path / "Subtitle Edit" / "_models" / "faster-whisper-small"
    ext_cache.mkdir(parents=True)
    for fn in ("model.bin", "config.json", "tokenizer.json"):
        (ext_cache / fn).write_bytes(b"ext")

    own_tiny = own_cache / "faster-whisper-tiny"
    own_tiny.mkdir(parents=True)
    for fn in ("model.bin", "config.json", "tokenizer.json"):
        (own_tiny / fn).write_bytes(b"own")

    monkeypatch.setattr(deps, "MODEL_CACHE", own_cache)
    monkeypatch.setattr(deps, "find_executable", lambda *_a: None)

    def fake_find_model(name, *_a):
        if name == "small":
            return ext_cache
        if name == "tiny":
            return own_tiny
        return None

    monkeypatch.setattr(deps, "find_model", fake_find_model)
    rows = {r["id"]: r for r in deps.inspect_whisper_models(SpeechSettings())}
    assert rows["small"]["installed"] and not rows["small"]["owned"]
    assert rows["tiny"]["installed"] and rows["tiny"]["owned"]

    # External model cannot be removed; owned model can be removed
    assert not deps.remove_owned_whisper_model("small")
    assert ext_cache.exists()
    assert deps.remove_owned_whisper_model("tiny")
    assert not own_tiny.exists()


def test_dependency_manager_ui_renders_in_main_window(tmp_path: Path, monkeypatch) -> None:
    app = _app()
    from autoclip_studio.database import Database
    from autoclip_studio.gui import MainWindow

    monkeypatch.setattr("autoclip_studio.gui.Database", lambda: Database(tmp_path / "deps.db"))
    window = MainWindow()
    try:
        idx = window.NAV_ITEMS.index("Dependencies")
        window.show_page(idx)
        app.processEvents()
        assert window.dependency_view.model_combo.count() >= 7
        assert window.dependency_view.check_btn.isEnabled()
        assert window.dependency_view.repair_btn.isEnabled()
    finally:
        window.close()
