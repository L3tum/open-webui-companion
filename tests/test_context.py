"""Endpoint tests for app.context: usage / compact / search / health.

Covers per-user scoping, the fail-closed static-token rule for chat-data
endpoints, input validation, and the mocked OWUI integration. The mocked
chat payloads follow the real v0.12.3 contract: the history lives at
``chat.history.messages`` (an id-keyed map whose active branch is the
``parentId`` chain ending at ``current_message_id``), server-computed
usage is the ``context_usage`` dict, and assistant text lives in
``output`` parts (see tests/conftest.py).
"""

from __future__ import annotations

import json
import uuid as uuid_module

import httpx
import pytest
import respx

from app.config import settings
from tests.conftest import (
    FWD_SECRET,
    OWUI_API,
    make_chat,
    make_context_usage,
    make_forward_jwt,
    make_message,
    make_user_jwt,
    mock_owui_identity,
)

CHAT_ID = str(uuid_module.uuid4())
OTHER_CHAT_ID = str(uuid_module.uuid4())
HOT_CHAT_ID = str(uuid_module.uuid4())
NO_USAGE_CHAT_ID = str(uuid_module.uuid4())

ALICE_TOKEN = make_user_jwt("u-alice")
ADMIN_TOKEN = make_user_jwt("u-admin")
BOGUS_TOKEN = make_user_jwt("u-unknown")


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


CHAT_MESSAGES = [
    make_message("m-1", "user", content="hello world", time=1716000000001),
    make_message(
        "m-2",
        "assistant",
        parent_id="m-1",
        output_text="hi there",
        model="llama3:8b",
        time=1716000000002,
    ),
    make_message(
        "m-3",
        "user",
        parent_id="m-2",
        content="needle in the haystack",
        time=1716000000003,
    ),
]
CHAT = make_chat(
    CHAT_ID,
    "Demo chat",
    CHAT_MESSAGES,
    context_usage=make_context_usage(tokens=1200, threshold=100000),
)

# A 90%-full chat (percent 90 > the 85% high-water mark) for compact tests.
HOT_CHAT = make_chat(
    HOT_CHAT_ID,
    "Hot chat",
    [
        make_message("h-1", "user", content="warm up", time=1716000000001),
        make_message(
            "h-2",
            "assistant",
            parent_id="h-1",
            output_text="it's getting hot in here",
            model="llama3:8b",
            time=1716000000002,
        ),
    ],
    context_usage=make_context_usage(tokens=90000, threshold=100000),
)

# Context compaction disabled on the instance: no context_usage dict.
NO_USAGE_CHAT = make_chat(
    NO_USAGE_CHAT_ID,
    "Bare chat",
    [make_message("n-1", "user", content="just one message", time=1716000000001)],
    context_usage=None,
)

OTHER_CHAT = make_chat(
    OTHER_CHAT_ID,
    "Other chat",
    [make_message("m-9", "user", content="another needle here", time=1716000000001)],
)


def _compact_response(**overrides) -> dict:
    result = {
        "ok": True,
        "compacted": True,
        "reason": None,
        "dropped_messages": 2,
        "kept_messages": 1,
        "summary_chars": 152,
        "context_usage": make_context_usage(tokens=400, threshold=100000),
    }
    result.update(overrides)
    return result


def _bearer(request):
    auth = request.headers.get("authorization", "")
    return auth[len("Bearer ") :] if auth.startswith("Bearer ") else ""


def _mock_chat_routes(rx, expected_token, chat):
    """Mock GET /chats/{id} and POST /chats/{id}/compact for one token;
    any other bearer gets a 403 (OWUI owner-scoping). Returns the compact
    route mock (for call assertions)."""

    def get_one(request):
        if _bearer(request) != expected_token:
            return httpx.Response(403, json={"detail": "You can't access this chat"})
        return httpx.Response(200, json=chat)

    def post_compact(request):
        if _bearer(request) != expected_token:
            return httpx.Response(403, json={"detail": "You can't access this chat"})
        return httpx.Response(200, json=_compact_response())

    rx.get(f"{OWUI_API}/chats/{chat['id']}").mock(side_effect=get_one)
    return rx.post(f"{OWUI_API}/chats/{chat['id']}/compact").mock(
        side_effect=post_compact
    )


def _mock_chat_search(rx, expected_token, items):
    """Mock OWUI's owner-scoped server-side search (GET /chats/search)."""

    def side_effect(request):
        if _bearer(request) != expected_token:
            return httpx.Response(403, json={"detail": "forbidden"})
        return httpx.Response(200, json=items)

    rx.get(f"{OWUI_API}/chats/search").mock(side_effect=side_effect)


def _mock_archived_count(rx, expected_token):
    def side_effect(request):
        if _bearer(request) != expected_token:
            return httpx.Response(403, json={"detail": "forbidden"})
        return httpx.Response(200, json={"count": 0})

    rx.get(f"{OWUI_API}/chats/archived/count").mock(side_effect=side_effect)


# ---------------------------------------------------------------------------
# /context/usage
# ---------------------------------------------------------------------------


def test_usage_ok(client, users):
    with respx.mock(assert_all_called=False) as rx:
        mock_owui_identity(rx, users)
        _mock_chat_routes(rx, ALICE_TOKEN, CHAT)
        resp = client.get(
            f"/context/usage?chat_id={CHAT_ID}", headers=_auth(ALICE_TOKEN)
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["chat_id"] == CHAT_ID
    assert body["title"] == "Demo chat"
    assert body["message_count"] == 3
    assert [m["message_id"] for m in body["latest_messages"]] == [
        "m-1",
        "m-2",
        "m-3",
    ]
    assert body["latest_messages"][1]["model"] == "llama3:8b"
    # Server-computed usage is reported verbatim (no client-side math).
    assert body["window_used_tokens"] == 1200
    assert body["window_limit_tokens"] == 100000
    assert body["window_percent"] == 1
    assert body["context_usage"]["source"] == "estimated"
    assert body["context_usage_note"] is None


def test_usage_without_context_usage(client, users):
    """context compaction disabled on the instance -> usage is null with an
    explanatory note, and the endpoint still reports message metadata."""
    with respx.mock(assert_all_called=False) as rx:
        mock_owui_identity(rx, users)
        _mock_chat_routes(rx, ALICE_TOKEN, NO_USAGE_CHAT)
        resp = client.get(
            f"/context/usage?chat_id={NO_USAGE_CHAT_ID}", headers=_auth(ALICE_TOKEN)
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["context_usage"] is None
    assert "unavailable" in body["context_usage_note"]
    assert body["window_used_tokens"] is None
    assert body["message_count"] == 1


def test_usage_tolerates_non_numeric_usage(client, users):
    chat = make_chat(
        str(uuid_module.uuid4()),
        "Weird",
        CHAT_MESSAGES,
        context_usage={"tokens": "weird", "threshold": None, "percent": "n/a"},
    )
    with respx.mock(assert_all_called=False) as rx:
        mock_owui_identity(rx, users)
        _mock_chat_routes(rx, ALICE_TOKEN, chat)
        resp = client.get(
            f"/context/usage?chat_id={chat['id']}", headers=_auth(ALICE_TOKEN)
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["window_used_tokens"] is None
    assert body["window_limit_tokens"] is None
    assert body["window_percent"] is None


def test_usage_chat_id_from_forwarded_header(client, users):
    with respx.mock(assert_all_called=False) as rx:
        mock_owui_identity(rx, users)
        _mock_chat_routes(rx, ALICE_TOKEN, CHAT)
        resp = client.get(
            "/context/usage",
            headers={**_auth(ALICE_TOKEN), "X-OpenWebUI-Chat-Id": CHAT_ID},
        )
    assert resp.status_code == 200
    assert resp.json()["chat_id"] == CHAT_ID


def test_usage_missing_chat_id_error_dict(client, users):
    with respx.mock(assert_all_called=False) as rx:
        mock_owui_identity(rx, users)
        resp = client.get("/context/usage", headers=_auth(ALICE_TOKEN))
    assert resp.status_code == 200
    body = resp.json()
    assert body["chat_id"] is None
    assert "chat_id is required" in body["error"]


def test_usage_bad_chat_id_400(client, users):
    with respx.mock(assert_all_called=False) as rx:
        mock_owui_identity(rx, users)
        resp = client.get(
            "/context/usage?chat_id=../../etc/passwd", headers=_auth(ALICE_TOKEN)
        )
    assert resp.status_code == 400
    assert "UUID" in resp.json()["detail"]


def test_usage_requires_forwarded_token(client, users, monkeypatch):
    """Chat-data endpoint: no Bearer token -> 401 even with a valid static
    OWUI_TOKEN configured (fail-closed)."""
    monkeypatch.setattr(settings, "OWUI_TOKEN", ADMIN_TOKEN)
    with respx.mock(assert_all_called=False) as rx:
        mock_owui_identity(rx, users)
        _mock_archived_count(rx, ADMIN_TOKEN)
        resp = client.get(f"/context/usage?chat_id={CHAT_ID}")
    assert resp.status_code == 401
    assert "forwarded" in resp.json()["detail"]


def test_usage_rejects_invalid_token(client, users, monkeypatch):
    """A token the backend rejects -> 401, never a silent static fallback."""
    monkeypatch.setattr(settings, "OWUI_TOKEN", ADMIN_TOKEN)
    with respx.mock(assert_all_called=False) as rx:
        mock_owui_identity(rx, users)
        resp = client.get(
            f"/context/usage?chat_id={CHAT_ID}", headers=_auth(BOGUS_TOKEN)
        )
    assert resp.status_code == 401


def test_usage_other_user_chat_graceful_error(client, users):
    """A chat owned by another user (OWUI 403) -> 200 + error dict."""
    with respx.mock(assert_all_called=False) as rx:
        mock_owui_identity(rx, users)
        _mock_chat_routes(rx, ADMIN_TOKEN, CHAT)  # not Alice's chat
        resp = client.get(
            f"/context/usage?chat_id={CHAT_ID}", headers=_auth(ALICE_TOKEN)
        )
    assert resp.status_code == 200
    assert "Open-WebUI API error 403" in resp.json()["error"]


# ---------------------------------------------------------------------------
# /context/search
# ---------------------------------------------------------------------------


def test_search_single_chat(client, users):
    with respx.mock(assert_all_called=False) as rx:
        mock_owui_identity(rx, users)
        _mock_chat_routes(rx, ALICE_TOKEN, CHAT)
        resp = client.get(
            f"/context/search?text=needle&chat_id={CHAT_ID}", headers=_auth(ALICE_TOKEN)
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["chat_id"] == CHAT_ID
    assert body["total"] == 1
    assert body["messages_scanned"] == 3
    assert body["page"] == 1
    assert body["page_size"] == 20
    match = body["matches"][0]
    assert match["message_id"] == "m-3"
    assert match["role"] == "user"
    assert match["matched_on"] == "content"
    assert match["occurrences"] == 1
    assert "needle" in match["snippet"]


def test_search_matches_assistant_output(client, users):
    """Assistant text lives in output parts, not content."""
    with respx.mock(assert_all_called=False) as rx:
        mock_owui_identity(rx, users)
        _mock_chat_routes(rx, ALICE_TOKEN, CHAT)
        resp = client.get(
            f"/context/search?text=hi+there&chat_id={CHAT_ID}",
            headers=_auth(ALICE_TOKEN),
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    match = body["matches"][0]
    assert match["message_id"] == "m-2"
    assert match["role"] == "assistant"
    assert match["matched_on"] == "output"


def test_search_case_insensitive(client, users):
    with respx.mock(assert_all_called=False) as rx:
        mock_owui_identity(rx, users)
        _mock_chat_routes(rx, ALICE_TOKEN, CHAT)
        resp = client.get(
            f"/context/search?text=NEEDLE&chat_id={CHAT_ID}", headers=_auth(ALICE_TOKEN)
        )
    assert resp.status_code == 200
    assert resp.json()["total"] == 1


def test_search_counts_repeated_hits(client, users):
    chat = make_chat(
        str(uuid_module.uuid4()),
        "Repeated",
        [make_message("r-1", "user", content="a needle and another needle", time=1)],
    )
    with respx.mock(assert_all_called=False) as rx:
        mock_owui_identity(rx, users)
        _mock_chat_routes(rx, ALICE_TOKEN, chat)
        resp = client.get(
            f"/context/search?text=needle&chat_id={chat['id']}",
            headers=_auth(ALICE_TOKEN),
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1  # one match per message
    assert body["matches"][0]["occurrences"] == 2


def test_search_paging(client, users):
    chat = make_chat(
        str(uuid_module.uuid4()),
        "Paged",
        [
            make_message(
                f"p-{i}",
                "user",
                content="the common word",
                time=i + 1,
                parent_id=None if i == 0 else f"p-{i - 1}",
            )
            for i in range(4)
        ],
    )
    with respx.mock(assert_all_called=False) as rx:
        mock_owui_identity(rx, users)
        _mock_chat_routes(rx, ALICE_TOKEN, chat)
        resp = client.get(
            f"/context/search?text=common&chat_id={chat['id']}&limit=2&page=2",
            headers=_auth(ALICE_TOKEN),
        )
    assert resp.status_code == 200
    body = resp.json()
    assert [m["message_id"] for m in body["matches"]] == ["p-2", "p-3"]
    assert body["total"] == 4  # total is all matches, not the page


def test_search_snippet_bounded_by_context_chars(client, users):
    chat = make_chat(
        str(uuid_module.uuid4()),
        "Snippet",
        [
            make_message(
                "s-1",
                "user",
                content="xxxxxxxxxxxxxxxxxxxxxx needle yyyyyyyyyyyyyy",
                time=1,
            )
        ],
    )
    with respx.mock(assert_all_called=False) as rx:
        mock_owui_identity(rx, users)
        _mock_chat_routes(rx, ALICE_TOKEN, chat)
        resp = client.get(
            f"/context/search?text=needle&chat_id={chat['id']}&context_chars=16",
            headers=_auth(ALICE_TOKEN),
        )
    assert resp.status_code == 200
    snippet = resp.json()["matches"][0]["snippet"]
    assert len(snippet) == 16
    assert "needle" in snippet


def test_search_cross_chat_uses_owui_search(client, users):
    """Without chat_id, OWUI's owner-scoped server-side search is used.
    (Strictly the caller's own chats; the server does the scoping.)"""
    items = [
        {
            "id": CHAT_ID,
            "title": "Demo chat",
            "snippet": "... needle in the haystack ...",
            "updated_at": 1716000000003,
            "archived": False,
        },
        {
            "id": OTHER_CHAT_ID,
            "title": "Other chat",
            "snippet": "... another needle here ...",
            "updated_at": 1716000000001,
            "archived": True,
        },
    ]
    with respx.mock(assert_all_called=False) as rx:
        mock_owui_identity(rx, users)
        _mock_chat_search(rx, ALICE_TOKEN, items)
        resp = client.get("/context/search?text=needle", headers=_auth(ALICE_TOKEN))
    assert resp.status_code == 200
    body = resp.json()
    assert body["query"] == "needle"
    assert body["chats_matched"] == 2
    assert body["has_more"] is False
    assert body["page_size"] == 60
    assert {r["chat_id"] for r in body["results"]} == {CHAT_ID, OTHER_CHAT_ID}
    assert body["results"][1]["archived"] is True


def test_search_cross_chat_has_more(client, users):
    items = [
        {"id": str(uuid_module.uuid4()), "title": f"c{i}", "snippet": "s"}
        for i in range(60)
    ]
    with respx.mock(assert_all_called=False) as rx:
        mock_owui_identity(rx, users)
        _mock_chat_search(rx, ALICE_TOKEN, items)
        resp = client.get("/context/search?text=x", headers=_auth(ALICE_TOKEN))
    assert resp.status_code == 200
    assert resp.json()["has_more"] is True


def test_search_requires_forwarded_token(client, users, monkeypatch):
    monkeypatch.setattr(settings, "OWUI_TOKEN", ADMIN_TOKEN)
    with respx.mock(assert_all_called=False):
        resp = client.get("/context/search?text=needle")
    assert resp.status_code == 401


def test_search_rejects_invalid_token(client, users, monkeypatch):
    monkeypatch.setattr(settings, "OWUI_TOKEN", ADMIN_TOKEN)
    with respx.mock(assert_all_called=False) as rx:
        mock_owui_identity(rx, users)
        resp = client.get("/context/search?text=needle", headers=_auth(BOGUS_TOKEN))
    assert resp.status_code == 401


@pytest.mark.parametrize(
    "params",
    [
        "text=needle&limit=0",
        "text=needle&limit=101",
        "text=needle&context_chars=9",
        "text=needle&context_chars=4001",
        "text=needle&page=0",
        "text=",
    ],
)
def test_search_input_clamps_422(client, users, params):
    with respx.mock(assert_all_called=False):
        resp = client.get(f"/context/search?{params}", headers=_auth(ALICE_TOKEN))
    assert resp.status_code == 422


def test_search_missing_text_422(client, users):
    with respx.mock(assert_all_called=False):
        resp = client.get("/context/search", headers=_auth(ALICE_TOKEN))
    assert resp.status_code == 422


def test_search_bad_chat_id_400(client, users):
    with respx.mock(assert_all_called=False) as rx:
        mock_owui_identity(rx, users)
        resp = client.get(
            "/context/search?text=needle&chat_id=not-a-uuid", headers=_auth(ALICE_TOKEN)
        )
    assert resp.status_code == 400


def test_search_owui_error_graceful_single_chat(client, users):
    with respx.mock(assert_all_called=False) as rx:
        mock_owui_identity(rx, users)
        rx.get(f"{OWUI_API}/chats/{CHAT_ID}").mock(
            return_value=httpx.Response(500, json={"detail": "boom"})
        )
        resp = client.get(
            f"/context/search?text=needle&chat_id={CHAT_ID}", headers=_auth(ALICE_TOKEN)
        )
    assert resp.status_code == 200
    assert "error" in resp.json()


def test_search_owui_error_graceful_cross_chat(client, users):
    with respx.mock(assert_all_called=False) as rx:
        mock_owui_identity(rx, users)
        rx.get(f"{OWUI_API}/chats/search").mock(
            return_value=httpx.Response(500, json={"detail": "boom"})
        )
        resp = client.get("/context/search?text=needle", headers=_auth(ALICE_TOKEN))
    assert resp.status_code == 200
    assert "error" in resp.json()


# ---------------------------------------------------------------------------
# /context/compact
# ---------------------------------------------------------------------------


def test_compact_ok(client, users):
    with respx.mock(assert_all_called=False) as rx:
        mock_owui_identity(rx, users)
        post_route = _mock_chat_routes(rx, ALICE_TOKEN, CHAT)
        resp = client.post(
            f"/context/compact?chat_id={CHAT_ID}", headers=_auth(ALICE_TOKEN)
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["compacted"] is True
    assert body["reason"] is None
    assert body["dropped_messages"] == 2
    assert body["kept_messages"] == 1
    assert body["summary_chars"] == 152
    assert body["context_usage"]["tokens"] == 400
    assert post_route.call_count == 1  # the compact POST was sent
    # No model override -> empty JSON body.
    assert json.loads(post_route.calls.last.request.content) == {}


def test_compact_forwards_model_override(client, users):
    with respx.mock(assert_all_called=False) as rx:
        mock_owui_identity(rx, users)
        post_route = _mock_chat_routes(rx, ALICE_TOKEN, CHAT)
        resp = client.post(
            f"/context/compact?chat_id={CHAT_ID}&model=llama3:8b",
            headers=_auth(ALICE_TOKEN),
        )
    assert resp.status_code == 200
    assert json.loads(post_route.calls.last.request.content) == {"model": "llama3:8b"}


def test_compact_dry_run(client, users):
    """dry_run inspects the chat without calling the compaction endpoint."""
    with respx.mock(assert_all_called=False) as rx:
        mock_owui_identity(rx, users)
        post_route = _mock_chat_routes(rx, ALICE_TOKEN, CHAT)
        resp = client.post(
            f"/context/compact?chat_id={CHAT_ID}&dry_run=true",
            headers=_auth(ALICE_TOKEN),
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["dry_run"] is True
    assert body["would_compact"] is True  # 3-message branch
    assert body["message_count"] == 3
    assert body["window_used_tokens"] == 1200
    assert "compacted" not in body
    assert post_route.call_count == 0  # no write happened


def test_compact_dry_run_short_branch(client, users):
    with respx.mock(assert_all_called=False) as rx:
        mock_owui_identity(rx, users)
        post_route = _mock_chat_routes(rx, ALICE_TOKEN, NO_USAGE_CHAT)
        resp = client.post(
            f"/context/compact?chat_id={NO_USAGE_CHAT_ID}&dry_run=true",
            headers=_auth(ALICE_TOKEN),
        )
    assert resp.status_code == 200
    assert resp.json()["would_compact"] is False
    assert post_route.call_count == 0


def test_compact_server_noop_passthrough(client, users):
    """The server decides whether compaction actually happens (disabled on
    the instance, branch too short, ...); the endpoint reports it, it
    does not assume."""
    with respx.mock(assert_all_called=False) as rx:
        mock_owui_identity(rx, users)

        def post_compact(request):
            return httpx.Response(
                200,
                json=_compact_response(
                    compacted=False,
                    reason="context compaction is disabled for this chat",
                    summary_chars=None,
                ),
            )

        rx.get(f"{OWUI_API}/chats/{CHAT_ID}").mock(
            side_effect=lambda r: httpx.Response(200, json=CHAT)
        )
        rx.post(f"{OWUI_API}/chats/{CHAT_ID}/compact").mock(side_effect=post_compact)
        resp = client.post(
            f"/context/compact?chat_id={CHAT_ID}", headers=_auth(ALICE_TOKEN)
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["compacted"] is False
    assert body["reason"] == "context compaction is disabled for this chat"


def test_compact_chat_not_found_graceful(client, users):
    """OWUI 403 on the chat (not the caller's) -> 200 + error dict."""
    with respx.mock(assert_all_called=False) as rx:
        mock_owui_identity(rx, users)
        _mock_chat_routes(rx, ADMIN_TOKEN, CHAT)  # not Alice's chat
        resp = client.post(
            f"/context/compact?chat_id={CHAT_ID}", headers=_auth(ALICE_TOKEN)
        )
    assert resp.status_code == 200
    assert "Open-WebUI API error 403" in resp.json()["error"]


def test_compact_missing_chat_id_error_dict(client, users):
    with respx.mock(assert_all_called=False) as rx:
        mock_owui_identity(rx, users)
        resp = client.post("/context/compact", headers=_auth(ALICE_TOKEN))
    assert resp.status_code == 200
    body = resp.json()
    assert body["chat_id"] is None
    assert "chat_id is required" in body["error"]


def test_compact_bad_chat_id_400(client, users):
    with respx.mock(assert_all_called=False) as rx:
        mock_owui_identity(rx, users)
        resp = client.post(
            "/context/compact?chat_id=../../etc/passwd", headers=_auth(ALICE_TOKEN)
        )
    assert resp.status_code == 400
    assert "UUID" in resp.json()["detail"]


def test_compact_requires_forwarded_token(client, users, monkeypatch):
    monkeypatch.setattr(settings, "OWUI_TOKEN", ADMIN_TOKEN)
    with respx.mock(assert_all_called=False):
        resp = client.post(f"/context/compact?chat_id={CHAT_ID}")
    assert resp.status_code == 401


def test_compact_rejects_invalid_token(client, users, monkeypatch):
    monkeypatch.setattr(settings, "OWUI_TOKEN", ADMIN_TOKEN)
    with respx.mock(assert_all_called=False) as rx:
        mock_owui_identity(rx, users)
        resp = client.post(
            f"/context/compact?chat_id={CHAT_ID}", headers=_auth(BOGUS_TOKEN)
        )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# /context/health (diagnostic: static fallback allowed)
# ---------------------------------------------------------------------------


def test_health_with_forwarded(client, users):
    with respx.mock(assert_all_called=False) as rx:
        mock_owui_identity(rx, users)
        _mock_archived_count(rx, ALICE_TOKEN)
        resp = client.get("/context/health", headers=_auth(ALICE_TOKEN))
    assert resp.status_code == 200
    body = resp.json()
    identity = body["identity"]
    assert identity["source"] == "forwarded"
    assert identity["email"] == "alice@example.com"
    assert identity["role"] == "pending"
    assert identity["user_id"] == "u-alice"
    assert body["context_endpoints"]["chat_id_header"] == "X-OpenWebUI-Chat-Id"


def test_health_static_fallback_redacted(client, users, monkeypatch):
    """Health is diagnostic: with no forwarded token it may use the static
    OWUI_TOKEN — but the resolved *static* identity is redacted on this
    unauthenticated endpoint (no email/role/user_id leakage)."""
    monkeypatch.setattr(settings, "OWUI_TOKEN", ADMIN_TOKEN)
    with respx.mock(assert_all_called=False) as rx:
        mock_owui_identity(rx, users)
        _mock_archived_count(rx, ADMIN_TOKEN)
        resp = client.get("/context/health")
    assert resp.status_code == 200
    identity = resp.json()["identity"]
    assert identity["source"] == "static"
    assert identity["redacted"] is True
    assert "email" not in identity
    assert "role" not in identity
    assert "user_id" not in identity


def test_health_no_credentials_500(client, users):
    with respx.mock(assert_all_called=False):
        resp = client.get("/context/health")
    assert resp.status_code == 500
    assert "No forwarded user token" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# cross-check enforcement at the endpoint layer
# ---------------------------------------------------------------------------


def test_endpoint_cross_check_mismatch_401(client, users, monkeypatch):
    """Signed header signed by the right secret but for a different user
    -> 401 (the static token is not a fallback)."""
    monkeypatch.setattr(settings, "OWUI_TOKEN", ADMIN_TOKEN)
    monkeypatch.setattr(settings, "OWUI_FORWARD_JWT_SECRET", FWD_SECRET)
    with respx.mock(assert_all_called=False) as rx:
        mock_owui_identity(rx, users)
        _mock_chat_routes(rx, ALICE_TOKEN, CHAT)
        resp = client.get(
            f"/context/usage?chat_id={CHAT_ID}",
            headers={
                **_auth(ALICE_TOKEN),
                "X-OpenWebUI-User-Jwt": make_forward_jwt(
                    sub="u-admin", email="admin@example.com", role="admin"
                ),
            },
        )
    assert resp.status_code == 401
    assert "mismatch" in resp.json()["detail"]


def test_endpoint_cross_check_missing_header_401(client, users, monkeypatch):
    """Secret configured but header stripped -> 401 (fail-closed)."""
    monkeypatch.setattr(settings, "OWUI_FORWARD_JWT_SECRET", FWD_SECRET)
    with respx.mock(assert_all_called=False):
        resp = client.get(
            f"/context/usage?chat_id={CHAT_ID}", headers=_auth(ALICE_TOKEN)
        )
    assert resp.status_code == 401


def test_endpoint_cross_check_pass(client, users, monkeypatch):
    monkeypatch.setattr(settings, "OWUI_FORWARD_JWT_SECRET", FWD_SECRET)
    with respx.mock(assert_all_called=False) as rx:
        mock_owui_identity(rx, users)
        _mock_chat_routes(rx, ALICE_TOKEN, CHAT)
        resp = client.get(
            f"/context/usage?chat_id={CHAT_ID}",
            headers={
                **_auth(ALICE_TOKEN),
                "X-OpenWebUI-User-Jwt": make_forward_jwt(
                    sub="u-alice", email="alice@example.com"
                ),
            },
        )
    assert resp.status_code == 200


def test_endpoint_cross_check_email_only_match(client, users, monkeypatch):
    """Header without a sub claim: the signed email alone ties the header
    to the caller -> accepted (either positive match suffices)."""
    monkeypatch.setattr(settings, "OWUI_FORWARD_JWT_SECRET", FWD_SECRET)
    with respx.mock(assert_all_called=False) as rx:
        mock_owui_identity(rx, users)
        _mock_chat_routes(rx, ALICE_TOKEN, CHAT)
        resp = client.get(
            f"/context/usage?chat_id={CHAT_ID}",
            headers={
                **_auth(ALICE_TOKEN),
                "X-OpenWebUI-User-Jwt": make_forward_jwt(
                    None, email="alice@example.com"
                ),
            },
        )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# legacy mode + removed endpoints
# ---------------------------------------------------------------------------


def test_legacy_mode_uses_static_for_chat_data(client, users, monkeypatch):
    """OWUI_ALLOW_USER_TOKEN=false (explicit opt-out): everything runs as
    the static token — the documented legacy behavior."""
    monkeypatch.setattr(settings, "OWUI_ALLOW_USER_TOKEN", False)
    monkeypatch.setattr(settings, "OWUI_TOKEN", ADMIN_TOKEN)
    with respx.mock(assert_all_called=False) as rx:
        mock_owui_identity(rx, users)
        _mock_chat_routes(rx, ADMIN_TOKEN, CHAT)
        resp = client.get(f"/context/usage?chat_id={CHAT_ID}")
    assert resp.status_code == 200
    assert resp.json()["chat_id"] == CHAT_ID


def test_config_endpoints_removed(client, users):
    """The admin-only config endpoints are gone (they were broken for
    everyone and the fix is to remove them, per review)."""
    with respx.mock(assert_all_called=False):
        assert client.get("/context/config").status_code == 404
        assert client.post("/context/config").status_code == 404
