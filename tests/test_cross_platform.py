from __future__ import annotations

import plistlib
import sys
from pathlib import Path
from types import SimpleNamespace

from autoclip_studio import dependencies as deps
from autoclip_studio.models import Highlight, Segment, Transcript
from autoclip_studio.paths import asset_path
from autoclip_studio.performance import HardwareSnapshot, resolve_profile_limits
from autoclip_studio.platform_utils import detect_platform, marker_matches
from autoclip_studio.process import ProcessManager
from autoclip_studio.rendering import detect_hw_encoder, render_clip
from autoclip_studio.speech_engine import SpeechSettings
from autoclip_studio.transcription import transcribe


def test_platform_detection_and_markers() -> None:
    info = detect_platform()
    assert info.system in {"windows", "macos", "linux"}
    assert marker_matches('sys_platform == "win32"', plat="win32", arch="AMD64")
    assert not marker_matches('sys_platform == "win32"', plat="darwin", arch="arm64")
    assert marker_matches('sys_platform == "darwin" and platform_machine == "arm64"', plat="darwin", arch="arm64")
    assert not marker_matches('sys_platform == "darwin" and platform_machine == "arm64"', plat="win32", arch="AMD64")


def test_macos_icns_and_app_bundle_staging(tmp_path: Path) -> None:
    icns = asset_path("autoclip.icns")
    assert icns.is_file()
    raw = icns.read_bytes()
    assert raw[:4] == b"icns"
    for chunk in (b"icp4", b"icp5", b"icp6", b"ic07", b"ic08", b"ic09", b"ic10"):
        assert chunk in raw

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packaging"))
    from build_macos import build_app_bundle

    app = build_app_bundle(tmp_path, compile_binary=False)
    plist_path = app / "Contents" / "Info.plist"
    assert plist_path.is_file()
    plist = plistlib.loads(plist_path.read_bytes())
    assert plist["CFBundleIdentifier"] == "com.autoclip.studio"
    assert plist["LSArchitecturePriority"] == ["arm64"]
    assert plist["NSHighResolutionCapable"] is True
    assert (app / "Contents" / "Resources" / "autoclip.icns").is_file()


def test_macos_dependency_filtering_excludes_windows_cuda() -> None:
    statuses = deps.inspect_all_dependencies(target_system="macos")
    ids = {s.spec.id for s in statuses}
    assert "mlx_whisper" in ids
    assert "cuda_runtime" not in ids


def test_apple_silicon_unified_memory_profile_limits() -> None:
    m2_snap = HardwareSnapshot(
        cpu_percent=30.0, cpu_physical=8, cpu_logical=8,
        ram_total_mib=16384, ram_used_mib=7000, ram_available_mib=9384,
        gpu_name="Apple M2 (Apple Silicon GPU)", unified_memory=True, gpu_backend="apple-silicon",
    )
    eco = resolve_profile_limits("eco", m2_snap)
    bal = resolve_profile_limits("balanced", m2_snap)
    turbo = resolve_profile_limits("turbo", m2_snap)

    assert eco.cpu_threads < bal.cpu_threads <= turbo.cpu_threads
    assert eco.whisper_batch_size <= bal.whisper_batch_size <= turbo.whisper_batch_size
    assert eco.nvenc_preset == "videotoolbox"
    assert eco.subprocess_priority_flags == 0


def test_videotoolbox_dispatch_and_cpu_fallback(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("autoclip_studio.rendering.sys.platform", "darwin")
    monkeypatch.setattr("autoclip_studio.rendering.videotoolbox_available", lambda _m: True)
    assert detect_hw_encoder(ProcessManager()) == "videotoolbox"

    commands: list[list[str]] = []

    class FakeMgr(ProcessManager):
        def run(self, command, **_kw):
            commands.append([str(c) for c in command])
            if "h264_videotoolbox" in command:
                import subprocess
                raise subprocess.CalledProcessError(1, command, output="vt init failed")
            return SimpleNamespace(stdout="", returncode=0)

    monkeypatch.setattr("autoclip_studio.rendering.validate_mp4", lambda *_a: 5.0)
    monkeypatch.setattr("autoclip_studio.rendering.find_media_tool", lambda n: n)
    out = tmp_path / "clip.mp4"
    dur = render_clip(
        tmp_path / "src.mp4", Highlight(0, 5, "T", "H", "D", 90),
        Transcript(5.0, "en", []), out, tmp_path / "work", (360, 640),
        "9:16", False, False, "Impact", True, FakeMgr(), lambda _m: None,
        dimensions=(1280, 720),
    )
    assert dur == 5.0
    assert any("h264_videotoolbox" in cmd for cmd in commands)
    assert any("libx264" in cmd for cmd in commands)


def test_mlx_whisper_dispatch_on_macos_with_fallback(tmp_path: Path, monkeypatch) -> None:
    media = tmp_path / "src.mp4"
    media.write_bytes(b"video")
    monkeypatch.setattr("autoclip_studio.transcription.sys.platform", "darwin")
    monkeypatch.setattr("autoclip_studio.transcription.mlx_whisper_available", lambda: True)
    monkeypatch.setattr("autoclip_studio.transcription.version", lambda pkg: "0.4.2" if pkg == "mlx-whisper" else "1.2.1")
    monkeypatch.setattr("autoclip_studio.transcription._audio_timing", lambda *_a: (0.0, 5.0, 5.0))
    monkeypatch.setattr(
        "autoclip_studio.transcription._run_mlx_whisper",
        lambda *_a: ([{"start": 0.0, "end": 3.0, "text": "Apple Silicon MLX test", "words": []}], "en", 5.0),
    )
    tr = transcribe(media, "large-v3-turbo", "auto", None, False, ProcessManager(), lambda _m: None, settings=SpeechSettings())
    assert tr.segments[0].text == "Apple Silicon MLX test"
