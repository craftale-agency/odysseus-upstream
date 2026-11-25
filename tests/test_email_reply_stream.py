import json
import pytest
from src.email_reply_stream import reply_body, stream_reply


def test_analysis_is_not_a_reply():
    assert reply_body('The user wants me to draft a reply. Context Analysis: ...', complete=True) == ''
    assert reply_body('<think>analysis</think>', complete=True) == ''
    assert reply_body('<<<REPLY>>>Hi Jonathan', complete=True) == ''
    assert reply_body('<<<REPLY>>><think>analysis</think>Hi<<<END>>>', complete=True) == ''
    assert reply_body('analysis\n<<<REPLY>>>Hi Jonathan,\nThanks.<<<END>>>', complete=True) == 'Hi Jonathan,\nThanks.'


def test_mangled_end_marker_is_salvaged():
    # A proxy between us and the model can delete the literal "<END>"
    # substring (channel-sentinel scrubbers match it with optional pipes),
    # so the model's <<<END>>> arrives as a bare bracket run. The reply
    # itself is complete — accept it with the debris stripped.
    assert reply_body('<<<REPLY>>>Hi Jack,\n\nThanks.\n\nBest,\nPietro\n<<>>', complete=True) == \
        'Hi Jack,\n\nThanks.\n\nBest,\nPietro'
    assert reply_body('<<<REPLY>>>Hi Jack,\n\nThanks.<<>', complete=True) == 'Hi Jack,\n\nThanks.'
    # Attached to the last word without whitespace, same story.
    assert reply_body('<<<REPLY>>>Thanks<<>>', complete=True) == 'Thanks'


def test_reply_without_any_closer_is_still_rejected():
    # No recognisable closer at all: either the budget truncated the reply
    # mid-generation or the model never closed the block. Do not guess it
    # was finished — the strict rejection is what protects the draft UI
    # from half-written emails.
    assert reply_body('<<<REPLY>>>Hi Jonathan,\nThanks.', complete=True) == ''
    assert reply_body('<<<REPLY>>>Truncated mid sente', complete=True) == ''
    # A lone mangled closer with no body is empty, not a reply.
    assert reply_body('<<<REPLY>>>\n<<>>', complete=True) == ''


@pytest.mark.asyncio
async def test_stream_filters_reasoning_and_split_markers(monkeypatch):
    from src import llm_core
    calls, events = [], []
    async def model(*args, **kwargs):
        calls.append(kwargs)
        for delta in ['Private analysis.', '<<<REP', 'LY>>>Hi Jonathan,', '\nThanks.', '<<<EN', 'D>>>']:
            yield 'data: ' + json.dumps({'delta': delta}) + '\n\n'
    async def emit(event): events.append(event)
    monkeypatch.setattr(llm_core, 'stream_llm', model)
    raw, used = await stream_reply([('http://local', 'Ajax', {})], [], emit)
    assert used == 'Ajax'
    assert calls[0]['thinking_mode'] == 'off'
    assert events[-1]['text'] == 'Hi Jonathan,\nThanks.'
    assert all('analysis' not in e['text'] and '<<<' not in e['text'] for e in events)


@pytest.mark.asyncio
async def test_incomplete_stream_clears_partial_draft(monkeypatch):
    from src import llm_core
    events = []
    async def model(*args, **kwargs):
        yield 'data: ' + json.dumps({'delta': '<<<REPLY>>>Partial'}) + '\n\n'
    async def emit(event): events.append(event)
    monkeypatch.setattr(llm_core, 'stream_llm', model)
    with pytest.raises(ValueError, match='incomplete'):
        await stream_reply([('http://local', 'Ajax', {})], [], emit)
    assert events[-1]['text'] == ''


@pytest.mark.asyncio
async def test_stream_accepts_reply_with_mangled_end_marker(monkeypatch):
    # End-to-end shape of the 2026-11-25 incident: the lane deletes "<END>"
    # from the model output, so the closer arrives as "<<>>" and the old
    # validator raised "Model returned analysis or an incomplete reply" on
    # every candidate. The salvaged reply must flow through untouched.
    from src import llm_core
    events = []
    async def model(*args, **kwargs):
        for delta in ['<<<REP', 'LY>>>Hi Jack,', '\n\nThanks.', '\n\nBest,\nPietro\n<<>>']:
            yield 'data: ' + json.dumps({'delta': delta}) + '\n\n'
    async def emit(event): events.append(event)
    monkeypatch.setattr(llm_core, 'stream_llm', model)
    raw, used = await stream_reply([('http://local', 'gemma4-fast:latest', {})], [], emit)
    assert used == 'gemma4-fast:latest'
    assert reply_body(raw, complete=True) == 'Hi Jack,\n\nThanks.\n\nBest,\nPietro'
    # The visible stream during generation matched the salvaged body (the
    # incremental view cuts at the first '<', which here is the junk closer).
    assert events[-1]['text'] == 'Hi Jack,\n\nThanks.\n\nBest,\nPietro'
