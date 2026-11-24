"""Pin the DI-bypass traps on the codex /emails wrapper.

Calling the real FastAPI handler directly (no DI) leaves every unset parameter
holding its truthy Query(...) default object. cached_only was already pinned;
date_from/date_to blew up in strptime (TypeError -> 500, masked as 401 by the
old bearer try/except) and refresh was silently dropped. These pin the
explicit None/0/forwarded values the wrapper must pass.
"""
from types import SimpleNamespace

from fastapi import APIRouter

import routes.codex_routes as codex_routes


def _build(captured):
    """Codex router whose email endpoints are kwargs-capturing stubs."""
    email_router = APIRouter()

    @email_router.get("/api/email/list")
    async def stub_list(**kwargs):
        captured.update(kwargs)
        return {"emails": [], "total": 0}

    router = codex_routes.setup_codex_routes(email_router=email_router)
    for route in router.routes:
        if route.path == "/api/codex/emails" and "GET" in route.methods:
            return route.endpoint
    raise AssertionError("GET /emails route not found")


def _api_request(owner="admin"):
    return SimpleNamespace(
        state=SimpleNamespace(
            api_token=True,
            api_token_scopes=["email:read"],
            api_token_owner=owner,
        )
    )


async def test_wrapper_passes_explicit_di_defaults():
    captured = {}
    endpoint = _build(captured)

    result = await endpoint(_api_request(), folder="INBOX", limit=5)

    assert result == {"emails": [], "total": 0}
    assert captured["date_from"] is None, "must not leak a truthy Query default"
    assert captured["date_to"] is None, "must not leak a truthy Query default"
    assert captured["cached_only"] == 0
    assert captured["cache_bust"] is None
    assert captured["refresh"] == 0
    assert captured["owner"] == "admin"


async def test_wrapper_forwards_refresh():
    captured = {}
    endpoint = _build(captured)

    await endpoint(_api_request(), folder="INBOX", limit=5, refresh=1)

    assert captured["refresh"] == 1
