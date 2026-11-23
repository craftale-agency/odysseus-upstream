import mcp_servers.email_server as es


def _forbid_index(monkeypatch):
    monkeypatch.setattr(es, "_fixture_list_emails", lambda *a, **k: None)
    monkeypatch.setattr(
        es, "_indexed_latest_emails",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("refresh=True must not read the header index")))
    monkeypatch.setattr(
        es, "_indexed_list_rows_by_uids",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("refresh=True must not reuse per-UID index rows")))


class _HeaderConn:
    """IMAP stub: SEARCH finds UID 7, FETCH serves one live header."""

    def select(self, folder, readonly=False):
        return "OK", []

    def uid(self, command, *args):
        if command == "SEARCH":
            return "OK", [b"7"]
        if command == "FETCH":
            return "OK", [
                (b"1 (UID 7 RFC822.HEADER {100}", _header_bytes("Fetched live")),
            ]
        raise AssertionError(f"unexpected IMAP command {command}")

    def logout(self):
        return None


def _header_bytes(subject: str) -> bytes:
    return (
        f"Subject: {subject}\r\n"
        "From: Sender <sender@example.com>\r\n"
        "Date: Wed, 09 Sep 2026 03:00:00 +0000\r\n"
        "Message-ID: <message@example.com>\r\n\r\n"
    ).encode()


def test_list_emails_refresh_true_skips_all_indexes(monkeypatch):
    """refresh=True bypasses both the latest-list index and per-UID index rows."""
    _forbid_index(monkeypatch)
    monkeypatch.setattr(es, "_imap_connect", lambda account=None: _HeaderConn())
    monkeypatch.setattr(es, "_get_cached_summaries", lambda: {})

    results = es._list_emails(refresh=True)

    assert [item["subject"] for item in results] == ["Fetched live"]


def test_list_emails_default_still_serves_index(monkeypatch):
    monkeypatch.setattr(es, "_fixture_list_emails", lambda *a, **k: None)
    monkeypatch.setattr(
        es, "_indexed_latest_emails",
        lambda *a, **k: [{"uid": "1", "subject": "From index"}])
    monkeypatch.setattr(
        es, "_imap_connect",
        lambda account=None: (_ for _ in ()).throw(
            AssertionError("index hit must not open IMAP")))

    assert es._list_emails()[0]["subject"] == "From index"


def test_across_accounts_refresh_bypasses_cache_and_forwards(monkeypatch):
    """refresh=True skips the merged-list memory cache and forwards per account."""
    rows = [
        {"id": "id-a", "name": "Alpha", "imap_user": "alpha@example.com"},
        {"id": "id-b", "name": "Beta", "imap_user": "beta@example.com"},
    ]
    monkeypatch.setattr(es, "_fixture_list_emails", lambda *a, **k: None)
    monkeypatch.setattr(es, "_list_accounts_raw", lambda: rows)
    _forbid_index(monkeypatch)
    stale_key = (
        es._current_owner(), "INBOX", 20, False, False, "", "",
        ("id-a", "id-b"),
    )
    es._EMAIL_LIST_CACHE[stale_key] = {
        "created": es.time.monotonic(),
        "results": [{"uid": "stale"}],
        "errors": [],
    }
    captured = {}

    def fake_list(**kwargs):
        captured[kwargs.get("account")] = kwargs.get("refresh")
        return [{"uid": "1", "subject": "Live"}]

    monkeypatch.setattr(es, "_list_emails", fake_list)

    results, errors = es._list_emails_across_accounts(refresh=True)

    assert not errors
    assert all(item["uid"] == "1" for item in results)
    assert captured == {"id-a": True, "id-b": True}


async def test_tool_schema_exposes_refresh_and_account_id():
    tools = await es.list_tools()
    tool = next(t for t in tools if t.name == "list_emails")
    props = tool.inputSchema["properties"]
    assert props["refresh"]["type"] == "boolean"
    assert "account_id" in props


async def test_call_tool_forwards_refresh_and_account_id(monkeypatch):
    """The handler forwards refresh and resolves the account_id alias."""
    monkeypatch.setattr(es, "_fixture_email_enabled", lambda: False)
    monkeypatch.setattr(
        es, "_list_accounts_raw",
        lambda: [{"id": "id-a", "name": "Alpha", "imap_user": "alpha@example.com"}])
    monkeypatch.setattr(es, "_load_config", lambda account=None: {})
    captured = {}

    def fake_list(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(es, "_list_emails", fake_list)

    await es.call_tool("list_emails",
                       {"folder": "INBOX", "refresh": True, "account_id": "id-a"})

    assert captured.get("refresh") is True
    assert captured.get("account") == "id-a"
