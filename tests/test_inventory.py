"""Endpoint tests for app.inventory (SilverBullet MCP-backed inventory).

The silverbullet-mcp Streamable HTTP endpoint is simulated with respx:
one route (POST ``MCP_URL``) dispatches on the JSON-RPC payload
(``initialize``, ``notifications/initialized``, ``tools/call``) and
returns ``structuredContent`` exactly like the deployed server does.

Key regression covered here: locations and items alike must be created
as regular ``.md`` notes (``inventory/office.md``) — the MCP server
rejects filenames without a ``.md`` extension and only lists ``.md``
files, so folder-style names (``inventory/office/``) are a 502.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from app import inventory as inventory_app
from app.config import settings

MCP_URL = "http://mcp.test/mcp"

SESSION_HEADERS = {"Mcp-Session-Id": "sess-test"}


@pytest.fixture(autouse=True)
def reset_shared_mcp_client():
    """The inventory module caches one MCP client per config; keep tests isolated."""
    inventory_app._reset_shared_client()
    yield
    inventory_app._reset_shared_client()


@pytest.fixture
def mcp_env(monkeypatch):
    """Configured SilverBullet MCP environment pointing at the mock server."""
    monkeypatch.setattr(settings, "SILVERBULLET_MCP_URL", MCP_URL)
    monkeypatch.setattr(settings, "SILVERBULLET_MCP_TOKEN", "test-sb-token")
    return settings


class MCPMock:
    """Simulates the silverbullet-mcp server behind one JSON-RPC endpoint."""

    def __init__(self, notes=None, note_contents=None, search_results=None):
        self.notes = list(notes or [])
        self.note_contents = dict(note_contents or {})
        self.search_results = list(search_results or [])
        self.create_calls: list[tuple[str, str]] = []  # (filename, content)
        self.create_error: str | None = None

    # -- JSON-RPC dispatch ------------------------------------------------

    def side_effect(self, request):
        payload = json.loads(request.content)
        method = payload.get("method")
        req_id = payload.get("id")

        if method == "initialize":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "serverInfo": {"name": "silverbullet-mcp", "version": "1.0.0"},
                    },
                },
                headers=SESSION_HEADERS,
            )
        if method == "notifications/initialized":
            return httpx.Response(202, content=b"")
        if method == "tools/call":
            name = payload["params"]["name"]
            args = payload["params"].get("arguments") or {}
            if name == "list-notes":
                return self._tool_response(req_id, {"notes": self.notes})
            if name == "create-note":
                if self.create_error:
                    return self._tool_error(req_id, self.create_error)
                self.create_calls.append(
                    (args.get("filename", ""), args.get("content", ""))
                )
                return self._tool_response(
                    req_id, {"created": True, "filename": args.get("filename")}
                )
            if name == "read-note":
                filename = args.get("filename", "")
                if filename not in self.note_contents:
                    return self._tool_error(req_id, f"Note not found: {filename}")
                return self._tool_response(
                    req_id,
                    {"filename": filename, "content": self.note_contents[filename]},
                )
            if name == "search-notes":
                return self._tool_response(req_id, {"results": self.search_results})
        raise AssertionError(f"Unexpected MCP method: {method}")

    # -- result shapes ------------------------------------------------------

    @staticmethod
    def _tool_response(req_id, structured):
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(structured)}],
                    "structuredContent": structured,
                    "isError": False,
                },
            },
        )

    @staticmethod
    def _tool_error(req_id, message):
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": message}],
                    "isError": True,
                },
            },
        )


# ─── locations are regular .md notes (regression) ────────────────────────


def test_add_location_creates_md_note(client, mcp_env):
    """AddInventory(type='location', name='Office') must call create-note
    with ``inventory/Office.md`` — not the folder-style ``inventory/office/``
    that the MCP server rejects with 'Filename must end with .md extension'.
    Case is preserved exactly as supplied.
    """
    mock = MCPMock(notes=[])
    with respx.mock(assert_all_called=False) as rx:
        rx.post(MCP_URL).mock(side_effect=mock.side_effect)
        resp = client.post(
            "/inventory/add", json={"type": "location", "name": "Office"}
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["created"] is True
    assert body["type"] == "location"
    assert body["path"] == "inventory/Office.md"
    assert len(mock.create_calls) == 1
    filename, content = mock.create_calls[0]
    assert filename == "inventory/Office.md"
    assert "type: location" in content
    assert "Office" in content


def test_add_location_is_idempotent_when_note_exists(client, mcp_env):
    mock = MCPMock(notes=["inventory/Office.md"])
    with respx.mock(assert_all_called=False) as rx:
        rx.post(MCP_URL).mock(side_effect=mock.side_effect)
        resp = client.post(
            "/inventory/add", json={"type": "location", "name": "Office"}
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["created"] is False
    assert body["path"] == "inventory/Office.md"
    assert not mock.create_calls


def test_add_location_reports_mcp_conflict(client, mcp_env):
    """Note already present server-side but not in the listing -> created=false."""
    mock = MCPMock(notes=[])
    mock.create_error = "Note already exists"
    with respx.mock(assert_all_called=False) as rx:
        rx.post(MCP_URL).mock(side_effect=mock.side_effect)
        resp = client.post(
            "/inventory/add", json={"type": "location", "name": "Office"}
        )
    assert resp.status_code == 200
    assert resp.json()["created"] is False


# ─── items ────────────────────────────────────────────────────────────────


def test_add_item_writes_md_note_under_location(client, mcp_env):
    mock = MCPMock(notes=["inventory/office.md"])
    with respx.mock(assert_all_called=False) as rx:
        rx.post(MCP_URL).mock(side_effect=mock.side_effect)
        resp = client.post(
            "/inventory/add",
            json={
                "name": "USB-C Cable",
                "location": "office",
                "properties": {"colour": "red", "cable length": "5 m"},
            },
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["type"] == "item"
    assert body["path"] == "inventory/office/USB-C Cable.md"
    assert body["location"] == "office"
    assert len(mock.create_calls) == 1
    filename, content = mock.create_calls[0]
    assert filename == "inventory/office/USB-C Cable.md"
    assert "type: item" in content
    assert "colour: red" in content


def test_add_item_missing_location_returns_404(client, mcp_env):
    mock = MCPMock(notes=[])
    with respx.mock(assert_all_called=False) as rx:
        rx.post(MCP_URL).mock(side_effect=mock.side_effect)
        resp = client.post(
            "/inventory/add", json={"name": "Gear", "location": "office"}
        )
    assert resp.status_code == 404
    assert "Create it first" in resp.json()["detail"]


def test_add_item_existing_without_overwrite_returns_409(client, mcp_env):
    mock = MCPMock(notes=["inventory/office.md", "inventory/office/USB-C Cable.md"])
    with respx.mock(assert_all_called=False) as rx:
        rx.post(MCP_URL).mock(side_effect=mock.side_effect)
        resp = client.post(
            "/inventory/add", json={"name": "USB-C Cable", "location": "office"}
        )
    assert resp.status_code == 409
    assert resp.json()["path"] == "inventory/office/USB-C Cable.md"
    assert not mock.create_calls


# ─── find ────────────────────────────────────────────────────────────────


def test_find_locations_returns_location_notes_only(client, mcp_env):
    """A note is a location when other notes live under it; leaf notes are items."""
    mock = MCPMock(
        notes=[
            "inventory/office.md",
            "inventory/office/box.md",
            "inventory/office/box/gear.md",
            "inventory/office/glasses.md",
            "unrelated-note.md",  # outside inventory/ -> ignored
        ]
    )
    with respx.mock(assert_all_called=False) as rx:
        rx.post(MCP_URL).mock(side_effect=mock.side_effect)
        resp = client.post("/inventory/find", json={"type": "location"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["type"] == "location"
    assert [r["path"] for r in body["results"]] == [
        "inventory/office.md",
        "inventory/office/box.md",
    ]
    assert [r["name"] for r in body["results"]] == ["office", "box"]
    assert [r["location"] for r in body["results"]] == ["", "office"]


def test_find_items_returns_only_leaf_notes(client, mcp_env):
    mock = MCPMock(
        notes=[
            "inventory/office.md",
            "inventory/office/box.md",
            "inventory/office/box/gear.md",
            "inventory/office/glasses.md",
        ]
    )
    with respx.mock(assert_all_called=False) as rx:
        rx.post(MCP_URL).mock(side_effect=mock.side_effect)
        resp = client.post("/inventory/find", json={"type": "item"})
    assert resp.status_code == 200
    body = resp.json()
    assert [r["path"] for r in body["results"]] == [
        "inventory/office/box/gear.md",
        "inventory/office/glasses.md",
    ]
    assert [(r["name"], r["location"]) for r in body["results"]] == [
        ("gear", "office/box"),
        ("glasses", "office"),
    ]


def test_find_items_search_returns_matching_item(client, mcp_env):
    mock = MCPMock(
        notes=[
            "inventory/office.md",
            "inventory/office/box.md",
            "inventory/office/box/gear.md",
        ],
        search_results=[
            {"filename": "inventory/office/box/gear.md", "snippet": "helmet inlays"}
        ],
    )
    with respx.mock(assert_all_called=False) as rx:
        rx.post(MCP_URL).mock(side_effect=mock.side_effect)
        resp = client.post("/inventory/find", json={"type": "item", "query": "inlays"})
    assert resp.status_code == 200
    body = resp.json()
    assert [r["name"] for r in body["results"]] == ["gear"]
    assert body["results"][0]["snippet"] == "helmet inlays"


# ─── take ────────────────────────────────────────────────────────────────


def test_take_item_marks_frontmatter(client, mcp_env):
    existing = (
        "---\ntype: item\ntitle: USB-C Cable\nlocation: office\n---\n\n# USB-C Cable\n"
    )
    mock = MCPMock(
        notes=["inventory/office/USB-C Cable.md"],
        note_contents={"inventory/office/USB-C Cable.md": existing},
    )
    with respx.mock(assert_all_called=False) as rx:
        rx.post(MCP_URL).mock(side_effect=mock.side_effect)
        resp = client.post("/inventory/take", json={"name": "USB-C Cable"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["taken"] is True
    assert body["path"] == "inventory/office/USB-C Cable.md"
    assert body["properties"].get("taken") == "true"
    assert "taken_at" in body["properties"]
    assert len(mock.create_calls) == 1
    filename, content = mock.create_calls[0]
    assert filename == "inventory/office/USB-C Cable.md"
    assert "taken: true" in content
    assert "taken_at:" in content


def test_take_location_is_rejected(client, mcp_env):
    resp = client.post("/inventory/take", json={"type": "location", "name": "office"})
    assert resp.status_code == 400
    assert "cannot be taken" in resp.json()["detail"].lower()


def test_take_unknown_item_returns_404(client, mcp_env):
    mock = MCPMock(notes=["inventory/office.md"])
    with respx.mock(assert_all_called=False) as rx:
        rx.post(MCP_URL).mock(side_effect=mock.side_effect)
        resp = client.post("/inventory/take", json={"name": "no-such-item"})
    assert resp.status_code == 404


# ─── health / configuration ──────────────────────────────────────────────


def test_health_ok(client, mcp_env):
    mock = MCPMock(notes=["inventory/office.md", "inventory/office/gear.md"])
    with respx.mock(assert_all_called=False) as rx:
        rx.post(MCP_URL).mock(side_effect=mock.side_effect)
        resp = client.get("/inventory/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["mcp_configured"] is True
    assert body["inventory"]["locations"] == ["office"]
    assert body["inventory"]["item_count"] == 1


def test_health_degraded_when_unconfigured(client, monkeypatch):
    monkeypatch.setattr(settings, "SILVERBULLET_MCP_URL", "")
    resp = client.get("/inventory/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["mcp_configured"] is False


def test_find_unconfigured_returns_500(client, monkeypatch):
    monkeypatch.setattr(settings, "SILVERBULLET_MCP_URL", "")
    resp = client.post("/inventory/find", json={})
    assert resp.status_code == 500
    assert "not configured" in resp.json()["detail"]
