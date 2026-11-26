"""Seam (e): the per-model context window reaches context budgeting.

Spec: openspec/changes/add-model-customization/specs/model-customization/spec.md
(requirement "Context window applies to Ollama-native calls through context
budgeting"). Discovery (`_query_context_length`) is replaced by a stub so no
endpoint is contacted; the per-model override is checked in front of it.

The typed turn resolution (`resolve_effective_context`) is covered by the
same seam: it shapes history and budgets recovery, so the saved window has
to out-vote the family table there as well.
"""

import json

import pytest

from core import database
from src import model_context
from src import model_customization as mc
from src.agent_runtime import context_resolution as cr
from src.agent_runtime.context_resolution import (
    ContextEvidence,
    ContextObservation,
    resolve_effective_context,
)
from tests.helpers.sqlite_db import make_temp_sqlite

LAB = "http://127.0.0.1:11434"
LAB_CHAT = LAB + "/api/chat"
GROQ = "https://api.groq.com/openai/v1"
GROQ_CHAT = GROQ + "/chat/completions"
DISCOVERED = 40960
FAMILY_TAG = "gemma4-thinker:latest"  # matches the 'gemma-4' table key: 262144


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(mc, "STORE_PATH", str(tmp_path / "model_customizations.json"))
    mc._reset_cache()
    factory, engine, tmpfile = make_temp_sqlite(database.Base.metadata)
    monkeypatch.setattr(mc, "_session_factory", lambda: factory)
    mc._reset_endpoint_cache()
    monkeypatch.setattr(model_context, "_query_context_length", lambda url, model: (DISCOVERED, True))
    monkeypatch.setattr(model_context, "_context_cache", {})
    yield factory
    engine.dispose()
    tmpfile.close()


@pytest.fixture
def customize(isolated):
    def add(endpoint_id, base_url, model, settings):
        with isolated() as db:
            db.add(database.ModelEndpoint(
                id=endpoint_id, name=endpoint_id, base_url=base_url,
                cached_models=json.dumps([model]),
            ))
            db.commit()
        mc._reset_endpoint_cache()
        mc.save_entry(endpoint_id, base_url, model, settings)
    return add


def test_ollama_receives_the_configured_context_window(customize):
    customize("lab-ollama", LAB, "qwen3:8b", {"num_ctx": 16384})
    assert model_context.get_context_length(LAB_CHAT, "qwen3:8b") == 16384


def test_budget_scaling_trusts_the_configured_context_window(customize):
    customize("lab-ollama", LAB, "qwen3:8b", {"num_ctx": 16384})
    assert model_context.get_context_length_known(LAB_CHAT, "qwen3:8b") == (16384, True)
    assert model_context.budget_context_for_model(LAB_CHAT, "qwen3:8b") == 16384


def test_a_stored_window_budgets_a_hosted_endpoint_without_a_request_field(isolated, tmp_path):
    with isolated() as db:
        db.add(database.ModelEndpoint(
            id="groq", name="groq", base_url=GROQ, cached_models=json.dumps(["llama-3.3-70b"]),
        ))
        db.commit()
    mc.save_entry("groq", GROQ, "llama-3.3-70b", {"num_ctx": 8192})
    assert model_context.get_context_length(GROQ_CHAT, "llama-3.3-70b") == 8192
    assert model_context.get_context_length_known(GROQ_CHAT, "llama-3.3-70b") == (8192, True)


def test_context_window_discovery_is_unchanged_elsewhere(customize):
    customize("lab-ollama", LAB, "qwen3:8b", {"temperature": 0.6})
    assert model_context.get_context_length(LAB_CHAT, "qwen3:8b") == DISCOVERED


def test_uncustomized_models_keep_discovered_context(customize):
    customize("lab-ollama", LAB, "qwen3:8b", {"temperature": 0.6})
    assert model_context.get_context_length(LAB_CHAT, "qwen3:8b") == DISCOVERED


def test_a_new_context_window_is_seen_without_clearing_the_discovery_cache(customize):
    customize("lab-ollama", LAB, "qwen3:8b", {"num_ctx": 16384})
    assert model_context.get_context_length(LAB_CHAT, "qwen3:8b") == 16384
    mc.save_entry("lab-ollama", LAB, "qwen3:8b", {"num_ctx": 32768})
    assert model_context.get_context_length(LAB_CHAT, "qwen3:8b") == 32768


@pytest.mark.asyncio
async def test_turn_resolution_trusts_the_configured_context_window(customize):
    customize("lab-ollama", LAB, FAMILY_TAG, {"num_ctx": 65536})
    assert model_context._lookup_known(FAMILY_TAG) == 262144
    resolution = await resolve_effective_context(LAB_CHAT, FAMILY_TAG, probe=False)
    assert (resolution.effective, resolution.evidence, resolution.source) == (
        65536, ContextEvidence.OPERATOR_DECLARED, "model_customization",
    )
    assert (resolution.budget_limit, resolution.shaping_window) == (65536, 65536)
    assert not resolution.mismatch


@pytest.mark.asyncio
async def test_turn_resolution_without_a_saved_window_keeps_the_family_table(isolated):
    resolution = await resolve_effective_context(LAB_CHAT, FAMILY_TAG, probe=False)
    assert (resolution.effective, resolution.evidence, resolution.source) == (
        262144, ContextEvidence.KNOWN_TABLE, "known_table",
    )


@pytest.mark.asyncio
async def test_turn_resolution_caps_a_measured_window_with_the_saved_one(customize, monkeypatch):
    customize("lab-ollama", LAB, FAMILY_TAG, {"num_ctx": 65536})

    async def advertised(endpoint_url, model, headers, deadline_seconds, is_local):
        return cr._ProbeResult(
            observations=(ContextObservation(ContextEvidence.PROVIDER_ADVERTISED, 262144, "models"),),
            io=True,
        )

    monkeypatch.setattr(cr, "probe_provider_context", advertised)
    resolution = await resolve_effective_context(LAB_CHAT, FAMILY_TAG)
    assert (resolution.effective, resolution.evidence, resolution.source) == (
        65536, ContextEvidence.OPERATOR_DECLARED, "model_customization",
    )
    assert not resolution.mismatch
