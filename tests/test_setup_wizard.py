from __future__ import annotations

import hashlib
import http.server
import json
import os
import socketserver
import subprocess
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import setup as setup_mod  # noqa: E402


def test_setup_redaction_and_persistent_setup_log(tmp_path: Path, monkeypatch) -> None:
    data_dir = tmp_path / "appdata"
    monkeypatch.setenv("AUTOCLIP_DATA_DIR", str(data_dir))
    monkeypatch.setattr(setup_mod, "ROOT", tmp_path)

    secret = "sk-proj-1234567890abcdefghijklmnop"
    gemini = "AIzaSyTestSecretKey1234567890abcdefghijk"
    setup_mod._append_setup_log(
        "ERROR",
        "core_packages",
        f"Authorization: Bearer abc12345 failed with {secret} and {gemini} https://user:pw@host.test/x",
    )
    log_path = setup_mod._setup_log_path()
    assert log_path.is_file()
    content = log_path.read_text(encoding="utf-8")
    assert secret not in content
    assert gemini not in content
    assert "user:pw@" not in content
    assert "[REDACTED]" in content


def test_setup_package_line_parser_and_ytdlp_newer_version_preserved() -> None:
    assert setup_mod._parse_pkg_line("Downloading PySide6-6.8.3-cp312.whl (42.1 MiB)")
    assert setup_mod._parse_pkg_line(" + yt-dlp==2026.12.01") == "Installed yt-dlp==2026.12.01"
    assert setup_mod._parse_pkg_line("Collecting faster-whisper==1.2.1")

    problems, specs = setup_mod.missing_or_outdated_requirements(
        installed_map={
            "pyside6": "6.8.3",
            "faster-whisper": "1.2.1",
            "av": "15.1.0",
            "yt-dlp": "2099.01.01",
            "opencv-python-headless": "4.10.0.84",
            "requests": "2.32.5",
            "keyring": "25.6.0",
            "platformdirs": "4.3.8",
            "pywin32": "312",
            "mlx-whisper": "0.9.0",
            "psutil": "7.2.2",
            "nvidia-ml-py": "13.615.71",
        }
    )
    assert problems == []
    assert specs == []


def test_setup_8_stages_monotonic_progress_cloud_only_and_state_persistence(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setenv("AUTOCLIP_DATA_DIR", str(data_dir))
    monkeypatch.setattr(setup_mod, "_JSON_PROGRESS", True)
    monkeypatch.setattr(setup_mod, "_LAST_OVERALL", 0.0)
    monkeypatch.setattr(setup_mod, "_find_uv", lambda: Path(sys.executable))
    monkeypatch.setattr(setup_mod, "media_tool_problems", lambda: [])
    monkeypatch.setattr(setup_mod, "_installed_versions_in_venv", lambda _py=None: {
        "pyside6": "6.8.3",
        "faster-whisper": "1.2.1",
        "av": "15.1.0",
        "yt-dlp": "2026.11.23",
        "opencv-python-headless": "4.10.0.84",
        "requests": "2.32.5",
        "keyring": "25.6.0",
        "platformdirs": "4.3.8",
        "pywin32": "312",
        "mlx-whisper": "0.4.2",
        "psutil": "7.2.2",
        "nvidia-ml-py": "13.615.71",
    })
    fake_py = tmp_path / "py.exe"
    fake_py.write_text("", encoding="utf-8")
    monkeypatch.setattr(setup_mod, "PYTHON", fake_py)
    monkeypatch.setattr(setup_mod, "PYTHONW", fake_py)
    monkeypatch.setattr(setup_mod, "_bootstrap_lock", setup_mod.contextlib.nullcontext)
    monkeypatch.setattr(setup_mod, "_probe_tool_version", lambda *_a, **_kw: "9.1")
    monkeypatch.setattr(
        setup_mod,
        "run",
        lambda *cmd, **_kw: "3.12.9 True" if "platform, sys" in " ".join(str(c) for c in cmd) else "ok",
    )

    summary = setup_mod.ensure_runtime(cloud_only=True, tests=False)
    assert summary["ok"] is True
    assert summary["failed_count"] == 0

    lines = [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.strip().startswith("{")]
    assert any(e["event"] == "system_info" for e in lines)
    assert any(e["event"] == "plan" and len(e["stages"]) == 8 for e in lines)

    overalls = [e["overall_percent"] for e in lines if isinstance(e.get("overall_percent"), (int, float))]
    assert overalls == sorted(overalls)
    # 100% must never appear before stage 8 completion / complete event
    non_final = [
        e["overall_percent"]
        for e in lines
        if e.get("overall_percent") is not None
        and not (e["event"] == "complete" or (e["event"] == "stage" and e.get("stage_id") == "final_validation" and e.get("state") == "Completed"))
    ]
    assert all(p < 100 for p in non_final)

    model_events = [e for e in lines if e["event"] == "progress" and e["id"] == "whisper_model_large_v3_turbo"]
    assert model_events and model_events[-1]["state"] == "Skipped"

    state_file = data_dir / "dependency_state.json"
    assert state_file.is_file()
    persisted = json.loads(state_file.read_text(encoding="utf-8"))
    assert persisted["last_setup_ok"] is True
    assert persisted["setup_summary"]["required_validated"] >= 6


def test_setup_resumable_download_partial_and_disk_space_guard(tmp_path: Path, monkeypatch) -> None:
    payload = b"SetupDownloadChunk-" * 2048
    digest = hashlib.sha256(payload).hexdigest()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            rng = self.headers.get("Range")
            if rng and rng.startswith("bytes="):
                start = int(rng.split("=")[1].split("-")[0])
                chunk = payload[start:]
                self.send_response(206)
                self.send_header("Content-Length", str(len(chunk)))
                self.send_header("Content-Range", f"bytes {start}-{len(payload) - 1}/{len(payload)}")
                self.end_headers()
                self.wfile.write(chunk)
            else:
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        def log_message(self, *_a):
            return

    server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        target = tmp_path / "tool.zip"
        part = target.with_suffix(".zip.part")
        part.write_bytes(payload[:512])
        setup_mod.download(f"http://127.0.0.1:{port}/tool.zip", target, digest, component="ffmpeg", stage_id="media_tools", stage_index=5)
        assert target.read_bytes() == payload
        assert not part.exists()

        monkeypatch.setattr(setup_mod.shutil, "disk_usage", lambda _p: type("U", (), {"free": 128})())
        with pytest.raises(RuntimeError, match="Insufficient disk space"):
            setup_mod.download(f"http://127.0.0.1:{port}/tool.zip", tmp_path / "full.zip", component="ffmpeg")
    finally:
        server.shutdown()
        server.server_close()


def test_setup_insufficient_disk_space_during_stage_1_fails_cleanly(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AUTOCLIP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(setup_mod, "_bootstrap_lock", setup_mod.contextlib.nullcontext)
    monkeypatch.setattr(setup_mod, "PYTHONW", tmp_path / "missing_pythonw.exe")
    monkeypatch.setattr(
        setup_mod,
        "_detect_system_info",
        lambda: {
            "os": "Windows",
            "os_detail": "Windows 11",
            "arch": "AMD64",
            "cpu": "Test CPU",
            "ram_bytes": 16 * 1024**3,
            "disk_free_bytes": 50 * 1024 * 1024,
            "gpu": "CPU",
            "hw_accel": "CPU fallback",
            "permissions_ok": True,
            "setup_log": str(tmp_path / "data" / "logs" / "setup.log"),
        },
    )
    with pytest.raises(RuntimeError, match="Insufficient disk space"):
        setup_mod.ensure_runtime(cloud_only=True, tests=False)


def test_system_info_cli_and_macos_launcher_source_contracts(tmp_path: Path) -> None:
    res = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "setup.py"), "--system-info"],
        cwd=ROOT, capture_output=True, text=True, timeout=15,
    )
    assert res.returncode == 0
    info = json.loads(res.stdout)
    assert {"os", "arch", "cpu", "ram_bytes", "disk_free_bytes", "gpu", "hw_accel", "permissions_ok"} <= set(info)

    launcher_m = (ROOT / "packaging" / "macos" / "Launcher.m").read_text(encoding="utf-8")
    for token in (
        "AutoClip Studio Setup",
        "fonts/poppins",
        "system_compatibility",
        "final_validation",
        "Launch Application",
        "Open Log",
        "Copy Diagnostics",
        "RedactSecrets",
    ):
        assert token in launcher_m
