"""Pin the empty_retry_attempts plumbing that cost two chat outages.

chat_routes passes empty_retry_attempts= to stream_llm (and the agent loop
forwards it to stream_llm_with_fallback -> stream_llm); the merge-window
THEIRS llm_core lacked the parameter, so every foreground chat TypeError'd.
These pin: the kwarg is accepted, empty streams retry the same model N
times, substantive output stops the retry loop.
"""
from unittest.mock import patch

from src import llm_core


DONE = 'data: [DONE]\n\n'
TEXT = 'data: {"delta": "hello"}\n\n'


def _inner_script(script):
    calls = {"n": 0}

    async def fake_inner(*args, **kwargs):
        i = calls["n"]
        calls["n"] += 1
        for chunk in script[min(i, len(script) - 1)]:
            yield chunk

    return fake_inner, calls


async def test_stream_llm_accepts_kwarg_and_retries_empty():
    script = [[DONE], [DONE], [TEXT, DONE]]  # two empties, then substantive
    fake, calls = _inner_script(script)
    with patch.object(llm_core, "_stream_model_resolve", lambda *a, **k: None, create=True), \
         patch.object(llm_core, "_local_model_slot", _null_slot), \
         patch.object(llm_core, "_stream_llm_inner", fake):
        chunks = [c async for c in llm_core.stream_llm(
            "http://x", "m", [], empty_retry_attempts=2)]
    assert calls["n"] == 3, "must retry same model on empty streams"
    assert TEXT in chunks


async def test_stream_llm_substantive_first_try_no_retry():
    fake, calls = _inner_script([[TEXT, DONE]])
    with patch.object(llm_core, "_local_model_slot", _null_slot), \
         patch.object(llm_core, "_stream_llm_inner", fake):
        _ = [c async for c in llm_core.stream_llm(
            "http://x", "m", [], empty_retry_attempts=3)]
    assert calls["n"] == 1


async def test_stream_llm_zero_retries_keeps_history():
    fake, calls = _inner_script([[DONE]])
    with patch.object(llm_core, "_local_model_slot", _null_slot), \
         patch.object(llm_core, "_stream_llm_inner", fake):
        _ = [c async for c in llm_core.stream_llm(
            "http://x", "m", [], empty_retry_attempts=0)]
    assert calls["n"] == 1


class _null_slot:
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False
