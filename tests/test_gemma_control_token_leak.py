"""Gemma 3/4 chat + tool protocol control tokens must never reach the chat.

Regression for the 2026-11-25 11:10 CET Odypezzus turn: gemma4-thinker answered
with the bare tool-result delimiter ``<|tool_response>``. It was streamed to the
browser and saved verbatim as the assistant message. The stream-level and the
persistence-level "visible text" filters must drop those tokens, while the
Qwen/Hermes ``<tool_call>`` tag stays untouched because the agent loop still
parses textual tool calls out of it.
"""

import pytest

from src.llm_core import (
    _strip_gemma_control_tokens,
    _strip_visible_chat_template_artifacts,
)
from src.agent_loop import _empty_response_fallback, _visible_response_text


@pytest.mark.parametrize(
    "raw",
    [
        "<|tool_response>",
        "<|tool_call>",
        "<tool_call|>",
        "<start_of_turn>",
        "<end_of_turn>",
    ],
)
def test_bare_control_tokens_are_stripped_everywhere(raw):
    assert _strip_gemma_control_tokens(raw) == ""
    assert _strip_visible_chat_template_artifacts(raw) == ""
    assert _visible_response_text(raw) == ""


def test_qwen_hermes_tool_call_tag_is_preserved():
    """`<tool_call>` (no pipe) is a textual tool call the agent loop must still see."""
    textual = '<tool_call>{"name": "manage_memory", "arguments": {"action": "list"}}</tool_call>'
    assert _strip_gemma_control_tokens(textual) == textual
    assert _strip_visible_chat_template_artifacts(textual) == textual


def test_prose_with_angle_brackets_is_untouched():
    prose = "If x < 2 then y > 1; compare a<b and <b>bold</b>."
    assert _strip_visible_chat_template_artifacts(prose) == prose


def test_text_around_control_tokens_is_kept():
    assert _visible_response_text("<|tool_response>Here is the feedback.<tool_call|>") == "Here is the feedback."


def test_control_only_round_becomes_the_empty_response_fallback():
    """>>> 16 raw chars reach the end of the loop as zero visible characters."""
    message, chunk = _empty_response_fallback("<|tool_response>", "", [])
    assert message == "The model returned an empty response. Please try again or switch to a different model."
    assert chunk and "empty response" in chunk


def test_incident_shape_is_not_visible():
    incident_saved_content = "<|tool_response>"  # exactly what chat_messages stored (16 chars)
    assert _visible_response_text(incident_saved_content) == ""
