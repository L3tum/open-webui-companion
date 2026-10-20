"""Endpoint tests for app.notes_sync (Open-WebUI v0.12.3 protocol).

Notes sync is a background / admin-triggered job: it always runs as the
static ``OWUI_TOKEN`` (there is no per-user token in scope). The mocks
cover the full protocol: paginated ``GET /notes`` (truncated ``data``),
per-note ``GET /notes/{id}``, ``POST /knowledge/{id}/sync/diff``,
multipart ``POST /files/``, ``POST /knowledge/{id}/file/add`` and
``POST /knowledge/{id}/sync/cleanup``.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from app.config import settings
from app.notes_sync import sync_status
from tests.conftest import OWUI_API, OWUI_BASE

STATIC_TOKEN = "sk-admin-key"
KB_ID = "kb-123"


def _static(request):
    auth = request.headers.get("authorization", "")
    return auth[len("Bearer ") :] if auth.startswith("Bearer ") else ""


def _full_note(note_id: str, title: str, markdown: str) -> dict:
    return {
        "id": note_id,
        "title": title,
        "data": {"content": {"md": markdown}},
    }


def _auth_headers_ok(request) -> bool:
    """Every OWUI call in a notes sync must carry the static token."""
    return _static(request) == STATIC_TOKEN


@pytest.fixture(autouse=True)
def reset_sync_state():
    """The sync status is a module global; never leak state between tests."""
    sync_status.last_sync = None
    sync_status.last_count = 0
    sync_status.last_error = None
    sync_status.manifest = {}
    yield
    sync_status.last_sync = None
    sync_status.last_count = 0
    sync_status.last_error = None
    sync_status.manifest = {}


@pytest.fixture
def notes_env(monkeypatch):
    """Static-token notes sync environment pointing at KB_ID."""
    monkeypatch.setattr(settings, "OWUI_TOKEN", STATIC_TOKEN)
    monkeypatch.setattr(settings, "NOTES_SYNC_KB_ID", KB_ID)
    return settings


def _register_notes(
    rx,
    summaries: list[dict],
    full_notes: list[dict],
):
    """Mock the paginated list + per-note fetch with static-token checks."""
    page = 1

    def list_notes(request):
        nonlocal page
        if not _auth_headers_ok(request):
            return httpx.Response(401, json={"detail": "unauthorized"})
        current_page = int(request.url.params.get("page", "1"))
        start = (current_page - 1) * 60
        return httpx.Response(200, json=summaries[start : start + 60])

    def get_note(request):
        if not _auth_headers_ok(request):
            return httpx.Response(401, json={"detail": "unauthorized"})
        note_id = request.url.path.rsplit("/", 1)[-1]
        note = next((n for n in full_notes if n["id"] == note_id), None)
        if note is None:
            return httpx.Response(404, json={"detail": "not found"})
        return httpx.Response(200, json=note)

    rx.get(f"{OWUI_API}/notes").mock(side_effect=list_notes)
    rx.get(f"{OWUI_API}/notes/", path__regex=r"/notes/[^/]+").mock(side_effect=get_note)


def _register_sync(
    rx,
    diff: dict,
    *,
    upload_ids: dict | None = None,
    upload_fail: str | None = None,
):
    """Mock diff / files / file-add / cleanup. Returns the route mocks."""
    diff = {
        "added": [],
        "modified": [],
        "deleted": [],
        "mkdir": [],
        "rmdir": [],
        "unmodified_count": 0,
        "directory_map": {},
        **diff,
    }
    upload_ids = upload_ids or {}

    def diff_route(request):
        if not _auth_headers_ok(request):
            return httpx.Response(401, json={"detail": "unauthorized"})
        return httpx.Response(200, json=diff)

    def upload_route(request):
        if not _auth_headers_ok(request):
            return httpx.Response(401, json={"detail": "unauthorized"})
        body = request.content.decode("utf-8", "replace")
        for filename, file_id in upload_ids.items():
            if f'name="file"; filename="{filename}"' in body:
                if upload_fail == filename:
                    return httpx.Response(500, json={"detail": "upload exploded"})
                return httpx.Response(
                    200, json={"id": file_id, "name": filename, "status": "completed"}
                )
        return httpx.Response(400, json={"detail": "unknown file"})

    def add_file(request):
        if not _auth_headers_ok(request):
            return httpx.Response(401, json={"detail": "unauthorized"})
        return httpx.Response(200, json={})

    def cleanup(request):
        if not _auth_headers_ok(request):
            return httpx.Response(401, json={"detail": "unauthorized"})
        return httpx.Response(200, json={})

    return {
        "diff": rx.post(f"{OWUI_API}/knowledge/{KB_ID}/sync/diff").mock(
            side_effect=diff_route
        ),
        "files": rx.post(f"{OWUI_API}/files/").mock(side_effect=upload_route),
        "file_add": rx.post(f"{OWUI_API}/knowledge/{KB_ID}/file/add").mock(
            side_effect=add_file
        ),
        "cleanup": rx.post(f"{OWUI_API}/knowledge/{KB_ID}/sync/cleanup").mock(
            side_effect=cleanup
        ),
    }


def _trigger(client, kb_id: str | None = None) -> httpx.Response:
    url = "/notes-sync/"
    if kb_id:
        url += f"?kb_id={kb_id}"
    return client.post(url, headers={"Authorization": f"Bearer {STATIC_TOKEN}"})


# ---------------------------------------------------------------------------
# sync protocol
# ---------------------------------------------------------------------------


def test_sync_uploads_added_files(client, notes_env):
    with respx.mock(assert_all_called=False) as rx:
        summaries = [{"id": "n-1", "title": "Alpha"}, {"id": "n-2", "title": "Beta"}]
        notes = [
            _full_note("n-1", "Alpha", "# Alpha\nalpha body"),
            _full_note("n-2", "Beta", "# Beta\nbeta body"),
        ]
        _register_notes(rx, summaries, notes)
        routes = _register_sync(
            rx,
            {
                "added": [
                    {"filename": "Alpha.md", "path": ""},
                    {"filename": "Beta.md", "path": ""},
                ],
                "unmodified_count": 0,
            },
            upload_ids={"Alpha.md": "file-a", "Beta.md": "file-b"},
        )
        resp = _trigger(client)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert body["total_notes"] == 2
    assert body["needs_upload"] == 2
    assert body["uploaded"] == ["Alpha.md", "Beta.md"]
    assert body["cleaned_up"] == []
    assert body.get("errors", []) == []
    # Protocol order: diff -> upload+add per file.
    assert routes["diff"].call_count == 1
    assert routes["files"].call_count == 2
    assert routes["file_add"].call_count == 2
    assert routes["cleanup"].call_count == 0
    # The uploaded file carried the note's markdown.
    uploaded = routes["files"].calls[0].request
    assert uploaded.headers["content-type"].startswith("multipart/form-data")
    assert b"alpha body" in uploaded.content
    # file/add linked the uploaded file ids to the KB.
    linked = [
        json.loads(c.request.content)["file_id"] for c in routes["file_add"].calls
    ]
    assert linked == ["file-a", "file-b"]


def test_sync_modified_reuploads_and_cleanup_deletes_stale(client, notes_env):
    with respx.mock(assert_all_called=False) as rx:
        summaries = [{"id": "n-1", "title": "Alpha"}, {"id": "n-2", "title": "Beta"}]
        notes = [
            _full_note("n-1", "Alpha", "changed alpha"),
            _full_note("n-2", "Beta", "beta body"),
        ]
        _register_notes(rx, summaries, notes)
        routes = _register_sync(
            rx,
            {
                "modified": [{"filename": "Alpha.md", "stale_file_id": "old-1"}],
                "deleted": [{"filename": "Old.md", "file_id": "old-2"}],
                "unmodified_count": 1,
            },
            upload_ids={"Alpha.md": "file-a2"},
        )
        resp = _trigger(client)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert body["uploaded"] == ["Alpha.md"]
    assert body["stale_files"] == 2
    assert body["cleaned_up"] == [
        "old-2",
        "old-1",
    ]  # deleted first, then modified stales
    assert routes["files"].call_count == 1  # only the modified note
    assert routes["cleanup"].call_count == 1
    cleanup_body = json.loads(routes["cleanup"].calls[0].request.content)
    assert sorted(cleanup_body["file_ids"]) == ["old-1", "old-2"]
    assert cleanup_body["dir_ids"] == []


def test_sync_duplicate_titles_get_unique_filenames(client, notes_env):
    with respx.mock(assert_all_called=False) as rx:
        summaries = [{"id": "n-1", "title": "Same"}, {"id": "n-2", "title": "Same"}]
        notes = [_full_note("n-1", "Same", "one"), _full_note("n-2", "Same", "two")]
        _register_notes(rx, summaries, notes)
        _register_sync(
            rx,
            {
                "added": [
                    {"filename": "Same.md", "path": ""},
                    {"filename": "Same_2.md", "path": ""},
                ],
            },
            upload_ids={"Same.md": "file-1", "Same_2.md": "file-2"},
        )
        resp = _trigger(client)
    assert resp.status_code == 200
    body = resp.json()
    assert body["uploaded"] == ["Same.md", "Same_2.md"]


def test_sync_no_notes_is_noop(client, notes_env):
    with respx.mock(assert_all_called=False) as rx:
        _register_notes(rx, [], [])
        routes = _register_sync(rx, {})
        resp = _trigger(client)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert body["total_notes"] == 0
    assert body["needs_upload"] == 0
    assert routes["diff"].call_count == 0  # no manifest, no diff


def test_sync_paginates_note_list(client, notes_env):
    summaries = [{"id": f"n-{i}", "title": f"T{i}"} for i in range(61)]
    notes = [
        _full_note(s["id"], s["title"], f"body {i}") for i, s in enumerate(summaries)
    ]
    with respx.mock(assert_all_called=False) as rx:
        _register_notes(rx, summaries, notes)
        _register_sync(rx, {"unmodified_count": 61})
        resp = _trigger(client)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_notes"] == 61  # both pages fetched
    assert body["needs_upload"] == 0


def test_sync_missing_kb_id_400(client, notes_env):
    notes_env.NOTES_SYNC_KB_ID = ""
    with respx.mock(assert_all_called=False) as rx:
        _register_notes(rx, [], [])
        resp = _trigger(client)
    assert resp.status_code == 400
    assert "Knowledge Base" in resp.json()["detail"]


def test_sync_explicit_kb_id_overrides_settings(client, notes_env):
    with respx.mock(assert_all_called=False) as rx:
        _register_notes(rx, [], [])
        resp = _trigger(client, kb_id="kb-other")
    assert resp.status_code == 200
    assert resp.json()["status"] == "success"


def test_sync_requires_static_token(client, monkeypatch):
    """No OWUI_TOKEN -> a clear error, never an empty-Bearer request."""
    monkeypatch.setattr(settings, "OWUI_TOKEN", "")
    monkeypatch.setattr(settings, "NOTES_SYNC_KB_ID", KB_ID)
    with respx.mock(assert_all_called=False) as rx:
        rx.get(f"{OWUI_API}/notes").mock(return_value=httpx.Response(200, json=[]))
        resp = client.post("/notes-sync/")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "error"
    assert "OWUI_TOKEN" in body["message"]


def test_sync_owui_error_is_reported(client, notes_env):
    with respx.mock(assert_all_called=False) as rx:
        _register_notes(rx, [{"id": "n-1", "title": "Alpha"}], [])
        # per-note fetch 404s -> sync aborts before any write
        resp = _trigger(client)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "error"
    assert "404" in body["message"]


def test_sync_upload_failure_is_not_fatal(client, notes_env):
    with respx.mock(assert_all_called=False) as rx:
        summaries = [{"id": "n-1", "title": "Alpha"}, {"id": "n-2", "title": "Beta"}]
        notes = [
            _full_note("n-1", "Alpha", "alpha"),
            _full_note("n-2", "Beta", "beta"),
        ]
        _register_notes(rx, summaries, notes)
        routes = _register_sync(
            rx,
            {
                "added": [
                    {"filename": "Alpha.md", "path": ""},
                    {"filename": "Beta.md", "path": ""},
                ],
            },
            upload_ids={"Alpha.md": "file-a", "Beta.md": "file-b"},
            upload_fail="Alpha.md",
        )
        resp = _trigger(client)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert body["uploaded"] == ["Beta.md"]
    assert len(body["errors"]) == 1
    assert body["errors"][0]["filename"] == "Alpha.md"
    assert "500" in body["errors"][0]["error"]
    # The failing file was not linked.
    assert routes["file_add"].call_count == 1


# ---------------------------------------------------------------------------
# status / health / debug
# ---------------------------------------------------------------------------


def test_status_endpoint(client, notes_env):
    resp = client.get("/notes-sync/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["last_sync"] is None
    assert body["kb_id"] == KB_ID


def test_health_requires_static_token(client, monkeypatch):
    monkeypatch.setattr(settings, "OWUI_TOKEN", "")
    resp = client.get("/notes-sync/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "error"
    assert "OWUI_TOKEN" in resp.json()["message"]


def test_health_ok(client, notes_env):
    with respx.mock(assert_all_called=False) as rx:
        rx.get(f"{OWUI_API}/notes").mock(
            side_effect=lambda r: httpx.Response(200, json=[])
        )
        resp = client.get("/notes-sync/health")
    assert resp.status_code == 200
    assert resp.json() == {
        "status": "ok",
        "instance": OWUI_BASE,
        "notes_accessible": True,
    }
