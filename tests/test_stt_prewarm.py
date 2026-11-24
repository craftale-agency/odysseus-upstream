"""Boot-time STT pre-warm (STTService.prewarm / prewarm_at_startup).

The pre-warm must drive the request path a real upload takes, send nothing
unless STT uses a remote endpoint, and never break startup.
"""
import asyncio
import io
import logging
import threading
import time
import wave
from types import SimpleNamespace

import httpx
import pytest

import src.database
from core.database import ModelEndpoint
from services.stt import stt_service as stt_module
from services.stt.stt_service import (
    PREWARM_ATTEMPTS,
    PREWARM_AUDIO,
    PREWARM_RETRY_DELAY_S,
    STTService,
    prewarm_at_startup,
    prewarm_enabled,
)
from tests.helpers.database import disposable_database

ENDPOINT_URL = "http://speaches.invalid/v1"
TRANSCRIBE_URL = ENDPOINT_URL + "/audio/transcriptions"


def _response(status_code=200, text=""):
    return httpx.Response(status_code, json={"text": text}, request=httpx.Request("POST", TRANSCRIBE_URL))


@pytest.fixture(autouse=True)
def retry_delays(monkeypatch):
    """Record the retry delay instead of sleeping for real."""
    delays = []
    monkeypatch.setattr(stt_module, "time", SimpleNamespace(sleep=delays.append, perf_counter=time.perf_counter))
    return delays


@pytest.fixture
def endpoint_db(monkeypatch, tmp_path):
    """A real ModelEndpoint row in a disposable DB, bound where _transcribe_api reads it."""
    with disposable_database(tmp_path) as factory:
        with factory() as db:
            db.add(ModelEndpoint(id="speaches", name="Speaches", base_url=ENDPOINT_URL, api_key="test-key"))
            db.commit()
        monkeypatch.setattr(src.database, "SessionLocal", factory)
        yield factory


@pytest.fixture
def stt_settings(monkeypatch):
    """Serve saved STT settings without touching settings.json."""
    def configure(service, **overrides):
        settings = {
            "stt_enabled": True,
            "stt_provider": "endpoint:speaches",
            "stt_model": "whisper-turbo",
            "stt_language": "",
        }
        settings.update(overrides)
        monkeypatch.setattr(service, "_load_settings", lambda: settings)

    return configure


@pytest.fixture
def posts(monkeypatch):
    """Capture transcription requests; each call consumes one canned reply."""
    calls = []
    replies = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(stt_module, "httpx", SimpleNamespace(post=fake_post))
    return calls, replies


def test_prewarm_audio_is_one_second_of_silent_wav():
    with wave.open(io.BytesIO(PREWARM_AUDIO)) as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 16000)
        assert wav.getnframes() == 16000
    assert set(PREWARM_AUDIO[44:]) == {0}
    assert len(PREWARM_AUDIO) < 64 * 1024


def test_prewarm_takes_exactly_the_path_a_real_upload_takes(endpoint_db, retry_delays, stt_settings, posts):
    calls, replies = posts
    replies.extend([_response(), _response(text="hello")])
    service = STTService()
    stt_settings(service)

    status = service.prewarm()
    assert service.transcribe(b"real webm bytes") == "hello"

    assert status.pop("duration_ms") >= 0
    assert status == {
        "status": "ok",
        "provider": "endpoint:speaches",
        "model": "whisper-turbo",
        "attempt": 1,
    }
    assert retry_delays == []
    assert len(calls) == 2
    (warm_url, warm_request), (real_url, real_request) = calls
    assert warm_url == real_url == TRANSCRIBE_URL
    assert warm_request["headers"] == real_request["headers"] == {"Authorization": "Bearer test-key"}
    assert warm_request["data"] == real_request["data"] == {"model": "whisper-turbo"}
    assert 0 < warm_request["timeout"] == real_request["timeout"] <= 120
    assert warm_request["files"]["file"][0] == "audio.webm"
    assert warm_request["files"]["file"][1].read() == PREWARM_AUDIO
    assert real_request["files"]["file"][1].read() == b"real webm bytes"


def test_prewarm_counts_an_empty_transcript_as_warmed(endpoint_db, stt_settings, posts):
    # Silence transcribes to "", which must not be mistaken for a failure.
    calls, replies = posts
    replies.append(_response(text=""))
    service = STTService()
    stt_settings(service)

    status = service.prewarm()

    assert (status["status"], status["attempt"]) == ("ok", 1)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "overrides, reason",
    [
        ({"stt_enabled": False}, "stt_disabled"),
        ({"stt_provider": "disabled"}, "provider_not_endpoint"),
        ({"stt_provider": "browser"}, "provider_not_endpoint"),
        ({"stt_provider": "local"}, "provider_not_endpoint"),
    ],
)
def test_prewarm_sends_nothing_without_a_remote_endpoint(monkeypatch, retry_delays, stt_settings, posts, overrides, reason):
    calls, _ = posts
    service = STTService()
    stt_settings(service, **overrides)
    monkeypatch.setattr(service, "_get_whisper", lambda: pytest.fail("prewarm must not load the local model"))

    status = service.prewarm()

    assert status == {
        "status": "skipped",
        "reason": reason,
        "provider": overrides.get("stt_provider", "endpoint:speaches"),
        "model": "whisper-turbo",
    }
    assert calls == []
    assert retry_delays == []


@pytest.mark.parametrize(
    "first_failure",
    [httpx.ConnectError("connection refused"), httpx.ReadTimeout("timed out"), _response(503)],
    ids=["connect-error", "read-timeout", "http-503"],
)
def test_prewarm_retries_once_after_a_failure(endpoint_db, retry_delays, stt_settings, posts, first_failure):
    calls, replies = posts
    replies.extend([first_failure, _response()])
    service = STTService()
    stt_settings(service)

    status = service.prewarm()

    assert (status["status"], status["attempt"]) == ("ok", PREWARM_ATTEMPTS)
    assert len(calls) == PREWARM_ATTEMPTS == 2
    assert retry_delays == [PREWARM_RETRY_DELAY_S]


@pytest.mark.parametrize(
    "failures",
    [
        [httpx.ReadTimeout("timed out"), httpx.ReadTimeout("timed out")],
        [_response(500), httpx.ConnectError("connection refused")],
    ],
    ids=["two-timeouts", "http-500-then-connect-error"],
)
def test_prewarm_gives_up_after_one_retry_without_raising(endpoint_db, retry_delays, stt_settings, posts, failures):
    calls, replies = posts
    replies.extend(failures)
    service = STTService()
    stt_settings(service)

    status = service.prewarm()

    assert status.pop("duration_ms") >= 0
    assert status == {
        "status": "failed",
        "provider": "endpoint:speaches",
        "model": "whisper-turbo",
        "attempt": PREWARM_ATTEMPTS,
    }
    assert len(calls) == PREWARM_ATTEMPTS
    assert retry_delays == [PREWARM_RETRY_DELAY_S]


@pytest.mark.parametrize(
    "status, level, line",
    [
        (
            {"status": "ok", "provider": "endpoint:x", "model": "m", "attempt": 1, "duration_ms": 4321},
            logging.INFO,
            "stt prewarm: ok model=m attempt=1 duration_ms=4321",
        ),
        (
            {"status": "failed", "provider": "endpoint:x", "model": "m", "attempt": 2, "duration_ms": 250123},
            logging.WARNING,
            "stt prewarm: failed model=m attempts=2 duration_ms=250123 (non-critical)",
        ),
        (
            {"status": "skipped", "reason": "stt_disabled", "provider": "disabled", "model": "base"},
            logging.INFO,
            "stt prewarm: skipped reason=stt_disabled",
        ),
    ],
)
async def test_startup_task_logs_one_outcome_line(caplog, status, level, line):
    with caplog.at_level(logging.DEBUG, logger="services.stt.stt_service"):
        await prewarm_at_startup(SimpleNamespace(prewarm=lambda: status))

    assert [record.levelno for record in caplog.records] == [level]
    assert caplog.messages == [line]


async def test_startup_task_does_not_block_the_event_loop():
    release = threading.Event()

    class BlockingService:
        def prewarm(self):
            release.wait(timeout=5)
            return {"status": "skipped", "reason": "stt_disabled"}

    task = asyncio.create_task(prewarm_at_startup(BlockingService()))
    # The loop must stay responsive while the warm-up request is in flight.
    await asyncio.wait_for(asyncio.sleep(0.05), timeout=1)
    assert not task.done()

    release.set()
    await asyncio.wait_for(task, timeout=5)


async def test_startup_task_logs_instead_of_raising(caplog):
    def boom():
        raise RuntimeError("settings unreadable")

    with caplog.at_level(logging.WARNING, logger="services.stt.stt_service"):
        await prewarm_at_startup(SimpleNamespace(prewarm=boom))

    assert caplog.messages == ["stt prewarm: error (non-critical): RuntimeError: settings unreadable"]


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, True),
        ("", True),
        ("1", True),
        ("yes", True),
        ("0", False),
        ("off", False),
        ("False", False),
    ],
)
def test_prewarm_is_on_by_default_and_can_be_switched_off(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("ODYSSEUS_STT_PREWARM", raising=False)
    else:
        monkeypatch.setenv("ODYSSEUS_STT_PREWARM", value)
    assert prewarm_enabled() is expected
