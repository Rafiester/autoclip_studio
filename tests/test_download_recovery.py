"""Regression tests for download pause, resume, persistent partials, HTTP 429 recovery, and auth suspension."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from autoclip_studio import download_state, media, youtube_metadata, youtube_subtitles
from autoclip_studio.database import Database
from autoclip_studio.download_state import (
    DEFAULT_AUTH_INSTRUCTIONS,
    DownloadState,
    download_coordinator,
    inspect_partial_files,
    load_download_state,
    verify_workspace_identity,
    workspace_path_for,
)
from autoclip_studio.models import ProjectOptions
from autoclip_studio.pipeline import Pipeline
from autoclip_studio.process import CancelledError, PausedError, ProcessManager
from autoclip_studio.ui.queue_workspace import ActiveJobCard, JobQueuePanel, JobRowCard

VIDEO_ID = "dSq0Z5XpoLc"
VIDEO_URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _reset_coordinator() -> None:
    download_coordinator().reset_for_tests()
    yield
    download_coordinator().reset_for_tests()


def _patch_media_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(media, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(media, "find_media_tool", lambda name: str(tmp_path / f"{name}.fake"))
    monkeypatch.setattr(media, "bundled_binary", lambda _name: None)
    monkeypatch.setattr("time.sleep", lambda _s: None)


# 1. Cancel during active download: clean process tree termination + explicit workspace cleanup
def test_cancel_active_download_cleans_process_tree_and_workspace(monkeypatch, tmp_path: Path) -> None:
    _patch_media_env(monkeypatch, tmp_path)
    manager = ProcessManager()
    workspace_seen: list[Path] = []

    def fake_run(command, **_kw):
        out_tpl = Path(command[command.index("-o") + 1])
        ws = out_tpl.parent
        workspace_seen.append(ws)
        (ws / f"{VIDEO_ID}.mp4.part").write_bytes(b"X" * 4096)
        manager.cancel()
        manager.check_cancelled()
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(manager, "run", fake_run)
    with pytest.raises(CancelledError) as exc_info:
        media.acquire_source(VIDEO_URL, True, manager, project_id="proj-cancel")
    assert not isinstance(exc_info.value, PausedError)
    assert len(workspace_seen) == 1
    assert not workspace_seen[0].exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group test")
def test_process_manager_terminates_child_process_group_on_cancel_and_pause(tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    script = (
        "import os, subprocess, sys, time; "
        f"child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
        f"open({str(pid_file)!r}, 'w').write(str(child.pid)); "
        "print('ready', flush=True); "
        "time.sleep(30)"
    )
    manager = ProcessManager()

    def _trigger_pause(line: str) -> None:
        if "ready" in line:
            manager.pause()

    with pytest.raises(PausedError):
        manager.run([sys.executable, "-c", script], on_output=_trigger_pause)

    child_pid = int(pid_file.read_text(encoding="utf-8").strip())
    # Give OS a tiny moment to reap the terminated child in the process group
    deadline = time.monotonic() + 3.0
    alive = True
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
            time.sleep(0.05)
        except ProcessLookupError:
            alive = False
            break
        except PermissionError:
            alive = False
            break
    assert not alive, f"Orphaned child process {child_pid} survived pause/killpg"


# 2. Pause and resume: preserves .part files & state, resumes in same workspace with --continue
def test_pause_and_resume_reuses_same_workspace_and_partials(monkeypatch, tmp_path: Path) -> None:
    _patch_media_env(monkeypatch, tmp_path)
    seen_workspaces: list[Path] = []
    seen_commands: list[list[str]] = []
    saved_states: list[DownloadState] = []

    manager1 = ProcessManager()

    def pause_run(command, **_kw):
        seen_commands.append(list(command))
        ws = Path(command[command.index("-o") + 1]).parent
        seen_workspaces.append(ws)
        (ws / f"{VIDEO_ID}.mp4.part").write_bytes(b"A" * 123456)
        manager1.pause()
        manager1.check_cancelled()
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(manager1, "run", pause_run)
    with pytest.raises(PausedError):
        media.acquire_source(VIDEO_URL, True, manager1, project_id="proj-pause", on_state=saved_states.append)

    ws = seen_workspaces[0]
    assert ws.is_dir()
    assert (ws / f"{VIDEO_ID}.mp4.part").is_file()
    persisted = load_download_state(ws)
    assert persisted is not None
    assert persisted.status == "paused"
    assert persisted.downloaded_bytes == 123456
    assert f"{VIDEO_ID}.mp4.part" in persisted.partial_files

    # Now resume in a fresh ProcessManager and verify exact workspace & --continue reuse
    manager2 = ProcessManager()
    partial_existed_before_resume: list[bool] = []

    def resume_run(command, **_kw):
        seen_commands.append(list(command))
        ws2 = Path(command[command.index("-o") + 1]).parent
        seen_workspaces.append(ws2)
        partial_existed_before_resume.append((ws2 / f"{VIDEO_ID}.mp4.part").is_file())
        out = Path(command[command.index("-o") + 1].replace("%(ext)s", "mp4"))
        receipt = Path(command[command.index("--print-to-file") + 2])
        out.write_bytes(b"completed-video-bytes")
        receipt.write_text(str(out) + "\n", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(manager2, "run", resume_run)
    final_path = media.acquire_source(VIDEO_URL, True, manager2, project_id="proj-pause")
    assert final_path.is_file()
    assert seen_workspaces[0] == seen_workspaces[1]
    assert partial_existed_before_resume == [True]
    assert "--continue" in seen_commands[1]
    assert "--part" in seen_commands[1]
    assert "--sleep-requests" in seen_commands[1]
    # Workspace cleaned up after atomic promotion to cache
    assert not ws.exists()


# 3. Partial file identity & format verification
def test_workspace_identity_verifies_video_and_format(tmp_path: Path) -> None:
    ws = workspace_path_for(VIDEO_URL, video_id=VIDEO_ID, cache_enabled=True, base_dir=tmp_path)
    (ws / f"{VIDEO_ID}.mp4.part").write_bytes(b"P" * 8192)
    download_state.save_download_state(
        ws,
        DownloadState(
            workspace_key=ws.name,
            workspace_dir=str(ws),
            video_id=VIDEO_ID,
            source_url=VIDEO_URL,
            selected_format=media.DEFAULT_FORMAT,
            status="paused",
            downloaded_bytes=8192,
        ),
    )

    reused, reason, preserved = verify_workspace_identity(
        ws, video_id=VIDEO_ID, source_url=VIDEO_URL, selected_format=media.DEFAULT_FORMAT,
    )
    assert reused is True
    assert reason == "verified_compatible"
    assert preserved == 8192

    # Format change cleans mismatched partial files safely
    reused2, reason2, preserved2 = verify_workspace_identity(
        ws, video_id=VIDEO_ID, source_url=VIDEO_URL, selected_format=media.FALLBACK_FORMAT,
    )
    assert reused2 is False
    assert reason2 == "format_mismatch"
    assert preserved2 == 0
    assert inspect_partial_files(ws) == (0, [])


# 4. Retry after network interruption reuses partials
def test_network_interruption_preserves_partials_for_retry(monkeypatch, tmp_path: Path) -> None:
    _patch_media_env(monkeypatch, tmp_path)
    manager = ProcessManager()
    attempts = 0
    seen_ws: list[Path] = []

    def flaky_run(command, **_kw):
        nonlocal attempts
        attempts += 1
        ws = Path(command[command.index("-o") + 1]).parent
        seen_ws.append(ws)
        part = ws / f"{VIDEO_ID}.mp4.part"
        part.write_bytes(b"N" * 32000)
        raise subprocess.CalledProcessError(1, command, output="ERROR: urlopen error [Errno 8] timed out")

    monkeypatch.setattr(manager, "run", flaky_run)
    with pytest.raises(media.DownloadError) as exc:
        media.acquire_source(VIDEO_URL, True, manager, project_id="proj-net")
    assert exc.value.category == "network_error"
    assert exc.value.partial_bytes == 32000
    assert (seen_ws[0] / f"{VIDEO_ID}.mp4.part").is_file()


# 5. HTTP 429 handling: immediate stop, Retry-After parsed, cooldown blocks premature retries
def test_http_429_stops_immediately_respects_retry_after_and_blocks_cooldown(monkeypatch, tmp_path: Path) -> None:
    _patch_media_env(monkeypatch, tmp_path)
    manager = ProcessManager()
    subprocess_calls = 0

    def rate_limited_run(command, **_kw):
        nonlocal subprocess_calls
        subprocess_calls += 1
        ws = Path(command[command.index("-o") + 1]).parent
        (ws / f"{VIDEO_ID}.mp4.part").write_bytes(b"R" * 50000)
        raise subprocess.CalledProcessError(
            1, command, output="ERROR: HTTP Error 429: Too Many Requests\nRetry-After: 45",
        )

    monkeypatch.setattr(manager, "run", rate_limited_run)
    with pytest.raises(media.DownloadError) as exc:
        media.acquire_source(VIDEO_URL, True, manager, project_id="proj-429")

    # CRITICAL: must stop on attempt 1 (zero aggressive 3x retry loop!)
    assert subprocess_calls == 1
    assert exc.value.category == "http_429"
    assert exc.value.partial_bytes == 50000
    assert exc.value.retry_after_seconds == pytest.approx(45.0, abs=1.0)
    assert exc.value.retry_after_epoch is not None

    # Premature retry during cooldown must NOT spawn another yt-dlp subprocess!
    with pytest.raises(media.DownloadError) as exc2:
        media.acquire_source(VIDEO_URL, True, manager, project_id="proj-429")
    assert subprocess_calls == 1
    assert exc2.value.category == "http_429"
    assert exc2.value.partial_bytes == 50000


def test_parse_retry_after_supports_seconds_and_hints() -> None:
    assert media.parse_retry_after("Retry-After: 120") == 120.0
    assert media.parse_retry_after("Please try again in 3 minutes") == 180.0
    assert media.parse_retry_after("No header here") is None


# 6. Authentication-required handling: suspends immediately, no bypass, blocks until explicit user retry
def test_auth_required_suspends_without_retry_until_explicit_user_retry(monkeypatch, tmp_path: Path) -> None:
    _patch_media_env(monkeypatch, tmp_path)
    manager = ProcessManager()
    calls = 0

    def bot_check_run(command, **_kw):
        nonlocal calls
        calls += 1
        ws = Path(command[command.index("-o") + 1]).parent
        (ws / f"{VIDEO_ID}.mp4.part").write_bytes(b"B" * 16384)
        raise subprocess.CalledProcessError(
            1, command, output="ERROR: [youtube] Sign in to confirm you're not a bot.",
        )

    monkeypatch.setattr(manager, "run", bot_check_run)
    with pytest.raises(media.DownloadError) as exc:
        media.acquire_source(VIDEO_URL, True, manager, project_id="proj-auth")
    assert calls == 1
    assert exc.value.category == "auth_required"
    assert exc.value.retryable is False
    assert exc.value.partial_bytes == 16384
    assert DEFAULT_AUTH_INSTRUCTIONS in exc.value.auth_instructions

    # Background/automatic retry without explicit_retry=True is blocked without spawning yt-dlp
    with pytest.raises(media.DownloadError) as exc2:
        media.acquire_source(VIDEO_URL, True, manager, project_id="proj-auth", explicit_retry=False)
    assert calls == 1
    assert exc2.value.category == "auth_required"

    # Explicit user retry after verification clears the suspension and attempts once
    def ok_run(command, **_kw):
        nonlocal calls
        calls += 1
        out = Path(command[command.index("-o") + 1].replace("%(ext)s", "mp4"))
        receipt = Path(command[command.index("--print-to-file") + 2])
        out.write_bytes(b"verified-video")
        receipt.write_text(str(out) + "\n", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(manager, "run", ok_run)
    path = media.acquire_source(VIDEO_URL, True, manager, project_id="proj-auth", explicit_retry=True)
    assert calls == 2
    assert path.is_file()


# 7. No duplicate concurrent subprocesses for the same job or video
def test_single_flight_lock_blocks_duplicate_concurrent_downloads(monkeypatch, tmp_path: Path) -> None:
    _patch_media_env(monkeypatch, tmp_path)
    started = threading.Event()
    release = threading.Event()
    manager1 = ProcessManager()
    manager2 = ProcessManager()

    def slow_run(command, **_kw):
        started.set()
        release.wait(timeout=5)
        out = Path(command[command.index("-o") + 1].replace("%(ext)s", "mp4"))
        receipt = Path(command[command.index("--print-to-file") + 2])
        out.write_bytes(b"video")
        receipt.write_text(str(out) + "\n", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(manager1, "run", slow_run)
    monkeypatch.setattr(manager2, "run", slow_run)

    result1: list[Path] = []
    t1 = threading.Thread(
        target=lambda: result1.append(media.acquire_source(VIDEO_URL, True, manager1, project_id="job-a")),
    )
    t1.start()
    assert started.wait(timeout=3)

    # Second concurrent request for the same video must be blocked immediately
    with pytest.raises(media.DownloadError) as conflict:
        media.acquire_source(VIDEO_URL, True, manager2, project_id="job-b")
    assert conflict.value.category == "duplicate_download"

    release.set()
    t1.join(timeout=5)
    assert len(result1) == 1 and result1[0].is_file()


# 8. Request optimization: in-flight metadata deduplication + no caching of 429 + subtitle cooldown skip
def test_metadata_extraction_deduplicates_concurrent_requests_and_never_caches_429(monkeypatch, tmp_path: Path) -> None:
    started = threading.Event()
    release = threading.Event()
    run_count = 0
    lock = threading.Lock()
    payload = json.dumps({
        "id": VIDEO_ID,
        "title": "Podcast Episode",
        "channel": "Studio",
        "duration": 600,
        "formats": [{"height": 1080, "vcodec": "avc1"}],
        "thumbnails": [],
    })

    def shared_run(_self, command, **_kw):
        nonlocal run_count
        with lock:
            run_count += 1
        started.set()
        release.wait(timeout=3)
        return subprocess.CompletedProcess(command, 0, payload + "\n", "")

    monkeypatch.setattr(ProcessManager, "run", shared_run)
    monkeypatch.setattr(youtube_metadata, "_fetch_thumbnail", lambda *_a, **_kw: b"")

    out_a: list = []
    out_b: list = []
    t1 = threading.Thread(target=lambda: out_a.append(youtube_metadata.fetch_youtube_metadata(VIDEO_ID, ProcessManager(), cache_dir=tmp_path)))
    t2 = threading.Thread(target=lambda: out_b.append(youtube_metadata.fetch_youtube_metadata(VIDEO_ID, ProcessManager(), cache_dir=tmp_path)))
    t1.start()
    assert started.wait(timeout=3)
    t2.start()
    time.sleep(0.05)
    release.set()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert run_count == 1
    assert out_a[0][0].title == "Podcast Episode"
    assert out_b[0][0].title == "Podcast Episode"

    # Temporary 429 must never be cached as permanent unavailability
    other_id = "abcdefghijk"
    def fail_429(_self, command, **_kw):
        raise subprocess.CalledProcessError(1, command, output="ERROR: HTTP Error 429: Too Many Requests\nRetry-After: 30")

    monkeypatch.setattr(ProcessManager, "run", fail_429)
    with pytest.raises(youtube_metadata.YouTubeMetadataError) as err:
        youtube_metadata.fetch_youtube_metadata(other_id, ProcessManager(), cache_dir=tmp_path)
    assert err.value.category == "http_429"
    assert err.value.temporary is True
    assert not (tmp_path / f"{other_id}.json").exists()

    # Subtitles must skip network during active cooldown instead of hammering YouTube
    sub_calls = 0
    def fail_sub(*_a, **_kw):
        nonlocal sub_calls
        sub_calls += 1
        raise AssertionError("Should not invoke yt-dlp for subtitles during 429 cooldown")

    monkeypatch.setattr(ProcessManager, "run", fail_sub)
    meta = youtube_metadata.YouTubeMetadata(
        video_id=other_id, title="T", channel="C", duration=100, resolution="1080p",
        view_count=10, upload_date=None, subtitle_languages=("en",),
    )
    assert youtube_subtitles.fetch_youtube_transcript(other_id, ProcessManager(), metadata=meta) is None
    assert sub_calls == 0


# 9. Restart persistence + UI controls (Pause/Resume/Rate Limited/Auth Required)
def test_database_restart_recovery_and_queue_ui_controls(tmp_path: Path) -> None:
    _app()
    db = Database(tmp_path / "app.db")
    ws = workspace_path_for(VIDEO_URL, video_id=VIDEO_ID, cache_enabled=True, base_dir=tmp_path)
    (ws / f"{VIDEO_ID}.mp4.part").write_bytes(b"Z" * 204800)
    dl_paused = DownloadState(
        workspace_key=ws.name,
        workspace_dir=str(ws),
        video_id=VIDEO_ID,
        source_url=VIDEO_URL,
        selected_format=media.DEFAULT_FORMAT,
        status="downloading",
        downloaded_bytes=204800,
        partial_files=[f"{VIDEO_ID}.mp4.part"],
    )
    download_state.save_download_state(ws, dl_paused)

    opts = ProjectOptions(source=VIDEO_URL, project_name="Podcast Ep 42")
    p_interrupted = db.create_project(VIDEO_URL, asdict(opts))
    db.update_project(p_interrupted, status="processing", progress=5, download_state=dl_paused.to_dict())

    future_epoch = time.time() + 120.0
    p_rate = db.create_project(VIDEO_URL, asdict(ProjectOptions(source=VIDEO_URL, project_name="Rate Limited Show")))
    db.update_project(
        p_rate,
        status="rate_limited",
        progress=6,
        download_state=DownloadState(
            workspace_key="yt-rate",
            workspace_dir=str(tmp_path / "missing-ws"),
            video_id="rate1234567",
            source_url="https://www.youtube.com/watch?v=rate1234567",
            status="rate_limited",
            downloaded_bytes=5242880,
            retry_after_epoch=future_epoch,
            cooldown_seconds=120.0,
            error_category="http_429",
        ).to_dict(),
    )

    p_auth = db.create_project(VIDEO_URL, asdict(ProjectOptions(source=VIDEO_URL, project_name="Bot Check Show")))
    db.update_project(
        p_auth,
        status="auth_required",
        progress=4,
        download_state=DownloadState(
            workspace_key="yt-auth",
            video_id="auth1234567",
            source_url="https://www.youtube.com/watch?v=auth1234567",
            status="auth_required",
            downloaded_bytes=1048576,
            error_category="auth_required",
            auth_instructions=DEFAULT_AUTH_INSTRUCTIONS,
        ).to_dict(),
    )

    # Simulate restart recovery
    recovered = db.recover_interrupted_projects()
    assert any(item["id"] == p_interrupted and item["status"] == "paused" for item in recovered)
    reloaded = db.get_project(p_interrupted)
    assert reloaded is not None and reloaded["status"] == "paused"
    # Cooldown restored into DownloadCoordinator
    rem, _ = download_coordinator().cooldown_remaining("rate1234567")
    assert rem > 100.0

    # ActiveJobCard shows Pause Download + Cancel Download in download mode
    paused_clicked = []
    card = ActiveJobCard(lambda: None, lambda: paused_clicked.append(True))
    card.start_job(reloaded, options_dict=asdict(opts), profile_name="balanced")
    card.set_stage_mode("download")
    assert not card.pause_button.isHidden()
    assert card.pause_button.isEnabled()
    assert card.pause_button.text() == "Pause Download"
    assert card.cancel_button.text() == "Cancel Download"
    card.pause_button.click()
    assert paused_clicked == [True]

    # JobQueuePanel renders Paused, Rate Limited (disabled during cooldown), and Auth Required rows cleanly
    panel = JobQueuePanel()
    panel.set_data(db.projects(), [])
    card_paused = panel._cards[p_interrupted]
    assert card_paused.primary_action_btn is not None
    assert card_paused.primary_action_btn.text() == "Resume Download"
    assert card_paused.primary_action_btn.isEnabled()

    card_rate = panel._cards[p_rate]
    assert card_rate.primary_action_btn is not None
    assert not card_rate.primary_action_btn.isEnabled()
    assert "Retry in" in card_rate.primary_action_btn.text()

    card_auth = panel._cards[p_auth]
    assert card_auth.primary_action_btn is not None
    assert card_auth.primary_action_btn.text() == "Retry After Verification"
    assert card_auth._recovery_banner_lbl is not None
    assert "verification" in card_auth._recovery_banner_lbl.text().lower()
