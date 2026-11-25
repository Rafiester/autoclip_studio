import json
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from threading import Event

import pytest
import requests

from autoclip_studio.config import SettingsStore
from autoclip_studio.database import Database
from autoclip_studio.models import ProjectOptions, Segment, Transcript
from autoclip_studio.pipeline import Pipeline
from autoclip_studio.process import CancelledError
from autoclip_studio.providers import AIProvider, ProviderError, _retry_after


def response(status, body, **headers):
    result = requests.Response()
    result.status_code = status
    result.encoding = "utf-8"
    result._content = (body if isinstance(body, str) else json.dumps(body)).encode("utf-8")
    result.headers.update(headers)
    return result


SUCCESS = {"choices": [{"message": {"content": "ok"}}]}


@pytest.fixture
def delays(monkeypatch):
    calls = []
    monkeypatch.setattr("autoclip_studio.providers.time.sleep", calls.append)
    monkeypatch.setattr("autoclip_studio.providers.random.uniform", lambda low, high: 0.25)
    return calls


def test_gateway_retries_are_capped_and_keep_full_redacted_errors(monkeypatch, delays):
    long_detail = "server diagnostic " * 100
    secret = "this-is-a-private-key"
    body = {"error": {"message": long_detail, "api_key": secret, "Authorization": "Bearer another-secret",
                      "headers": {"Proxy-Authorization": ["opaque-secret"]}}}
    statuses = iter([503, 502, 504, 503, 504])
    calls = []
    warnings = []

    def post(_url, **kwargs):
        status = next(statuses)
        calls.append(status)
        return response(status, body)

    monkeypatch.setattr(requests, "post", post)
    provider = AIProvider("openai", secret, "https://provider.invalid", "primary", notify=warnings.append)
    with pytest.raises(ProviderError) as failure:
        provider.complete("system", "prompt")
    assert failure.value.category == "overload"
    assert len(calls) == 5
    assert delays == [1.25, 2.25, 4.25, 8.25]
    assert len(warnings) == 4
    for message in [*warnings, str(failure.value)]:
        assert long_detail in message
        assert all(value not in message for value in (secret, "another-secret", "opaque-secret"))


def test_rate_limit_honors_retry_after(monkeypatch, delays):
    replies = iter([response(429, "Slow down", **{"Retry-After": "7"}), response(200, SUCCESS)])
    monkeypatch.setattr(requests, "post", lambda *_args, **_kwargs: next(replies))
    assert AIProvider("openai", "private-value", "https://provider.invalid", "primary").test() == "ok"
    assert delays == [7]
    date = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=20), usegmt=True)
    assert 18 < _retry_after(date) <= 20


@pytest.mark.parametrize("enabled", [False, True])
def test_zero_quota_skips_retries_and_fallback_stays_in_provider(monkeypatch, delays, enabled):
    calls = []
    primary_error = "Quota exceeded for metric: requests, limit: 0."

    def post(url, **kwargs):
        model = kwargs["json"]["model"]
        calls.append((url, model))
        if model == "primary":
            return response(429, primary_error, **{"Retry-After": "99"})
        return response(200, SUCCESS)

    monkeypatch.setattr(requests, "post", post)
    provider = AIProvider("custom", "private-value", "https://provider.invalid/v1", "primary",
                          fallback_models=("primary", "secondary", "secondary"), auto_fallback=enabled)
    if enabled:
        assert provider.complete("system", "prompt") == "ok"
        assert provider.complete("system", "second chunk") == "ok"
        assert [model for _, model in calls] == ["primary", "secondary", "secondary"]
    else:
        with pytest.raises(ProviderError, match="Quota exhausted"):
            provider.complete("system", "prompt")
        assert len(calls) == 1
    assert {url for url, _ in calls} == {"https://provider.invalid/v1/chat/completions"}
    assert delays == []


@pytest.mark.parametrize(("status", "body"), [(401, "Invalid authentication"), (400, "API key not valid")])
def test_invalid_credentials_are_reported_without_model_fallback(monkeypatch, delays, status, body):
    calls = []

    def post(_url, **kwargs):
        calls.append(kwargs["json"]["model"])
        return response(status, body)

    monkeypatch.setattr(requests, "post", post)
    provider = AIProvider("openai", "private-value", "https://provider.invalid", "primary",
                          fallback_models=("secondary",), auto_fallback=True)
    with pytest.raises(ProviderError, match="Invalid credentials"):
        provider.complete("system", "prompt")
    assert calls == ["primary"]
    assert delays == []


def test_network_failure_does_not_log_requests_exception_details(monkeypatch):
    def post(*_args, **_kwargs):
        raise requests.ConnectionError("secret token in network error")

    monkeypatch.setattr(requests, "post", post)
    with pytest.raises(ProviderError, match="Network failure") as failure:
        AIProvider("openai", "private-value", "https://provider.invalid", "primary").test()
    assert "secret token" not in str(failure.value)


def test_cancellation_interrupts_retry_delay(monkeypatch):
    event = Event()
    monkeypatch.setattr(requests, "post", lambda *_args, **_kwargs: response(503, "Overloaded"))
    provider = AIProvider("openai", "private-value", "https://provider.invalid", "primary",
                          cancel_event=event, notify=lambda _message: event.set())
    with pytest.raises(CancelledError):
        provider.complete("system", "prompt")


def test_gemini_fallback_reuses_the_uploaded_video(monkeypatch, tmp_path, delays):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"original video")
    calls = []
    deleted = []
    upload_url = "https://upload.invalid/session"

    def post(url, **kwargs):
        calls.append(url)
        if url.endswith("/files"):
            return response(200, {}, **{"X-Goog-Upload-URL": upload_url})
        if url == upload_url:
            assert kwargs["data"].read() == b"original video"
            return response(200, {"file": {"state": "ACTIVE", "name": "files/example", "uri": "video-uri"}})
        assert kwargs["json"]["contents"][0]["parts"][0]["fileData"]["fileUri"] == "video-uri"
        if url.endswith("/primary:generateContent"):
            return response(429, {"error": {"details": [{"quotaValue": "0"}]}})
        return response(200, {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]})

    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr(requests, "delete", lambda url, **_kwargs: deleted.append(url))
    provider = AIProvider("gemini", "private-value", "https://provider.invalid/v1beta", "primary",
                          fallback_models=("models/primary", "models/secondary"), auto_fallback=True)
    assert provider.complete_video(source, "prompt") == "ok"
    assert calls == [
        "https://provider.invalid/upload/v1beta/files", upload_url,
        "https://provider.invalid/v1beta/models/primary:generateContent",
        "https://provider.invalid/v1beta/models/secondary:generateContent",
    ]
    assert deleted == ["https://provider.invalid/v1beta/files/example"]
    assert delays == []


def test_ai_retry_resumes_from_disk_with_cache_disabled(monkeypatch, tmp_path):
    monkeypatch.setattr("autoclip_studio.pipeline.CACHE_DIR", tmp_path / "cache")
    options = ProjectOptions(source="https://www.youtube.com/watch?v=example", provider="openai",
                             model="primary", cache_enabled=False, output_dir=str(tmp_path / "exports"))
    db = Database(tmp_path / "projects.db")
    settings = SettingsStore(tmp_path / "settings.json")
    monkeypatch.setattr(settings, "api_key", lambda _provider: "private-value")
    project_id = db.create_project(options.source, asdict(options))
    calls = []

    def acquire(*args, job_dir, **_kwargs):
        calls.append("download")
        prepared = job_dir / "source.mp4"
        prepared.write_bytes(b"prepared media")
        return prepared

    def transcribe(*_args):
        calls.append("transcript")
        return Transcript(60, "en", [Segment(0, 40, "A prepared transcript.")])

    def render(source, _highlight, transcript, output, *_args, **_kwargs):
        assert source.is_file()
        assert transcript.segments[0].text == "A prepared transcript."
        output.touch()
        return 30

    replies = iter([
        response(429, "insufficient_quota"),
        response(429, "insufficient_quota"),
        response(200, {"choices": [{"message": {"content": '{"highlights":[{"start":0,"end":30}]}'}}]}),
    ])
    monkeypatch.setattr("autoclip_studio.pipeline.acquire_source", acquire)
    monkeypatch.setattr("autoclip_studio.pipeline.source_dimensions", lambda *_args: (1280, 720))
    monkeypatch.setattr("autoclip_studio.pipeline.transcribe", transcribe)
    monkeypatch.setattr("autoclip_studio.pipeline.render_clip", render)
    monkeypatch.setattr(requests, "post", lambda *_args, **_kwargs: next(replies))
    progress = []
    report = lambda value, _message: progress.append(value)

    with pytest.raises(ProviderError):
        Pipeline(db, settings).run(project_id, options, report)
    job = tmp_path / "cache" / "jobs" / project_id
    assert (job / "analysis.json").is_file()
    assert (job / "source.mp4").is_file()
    assert db.projects()[0]["status"] == "analysis_failed"
    assert db.projects()[0]["progress"] == progress[-1] == 40

    with pytest.raises(ProviderError):
        Pipeline(db, settings).run(project_id, options, report, resume_analysis=True)
    assert db.projects()[0]["status"] == "analysis_failed"
    assert len(Pipeline(db, settings).run(project_id, options, report, resume_analysis=True)) == 1
    assert calls == ["download", "transcript"]
    assert db.projects()[0]["status"] == "completed"
    assert len(db.projects()) == 1
    assert not job.exists()
