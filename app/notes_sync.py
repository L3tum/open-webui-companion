"""Notes-to-Knowledge-Base sync for Open-WebUI.

Follows the Open-WebUI **v0.12.3** API:

- Notes: ``GET /notes`` (60 per page; the list response *truncates*
  ``data``) and ``GET /notes/{id}`` for the full note. The note's
  markdown body lives at ``data.content.md`` (the field the OWUI
  editor exports).
- Knowledge sync protocol: ``POST /knowledge/{id}/sync/diff`` with a
  manifest of ``{filename, path, checksum, size}`` entries, then — for
  each added/modified entry — a multipart upload to ``POST /files/``
  plus ``POST /knowledge/{id}/file/add``, and finally
  ``POST /knowledge/{id}/sync/cleanup`` with the stale file/directory
  ids returned by the diff.

Notes sync is a background / admin-triggered job with no per-user
token, so it always runs as the static ``OWUI_TOKEN``; a missing token
is a ConfigError, never a silent empty Bearer.
"""

import asyncio
import hashlib
import json
import re
from datetime import UTC, datetime

import httpx
from fastapi import APIRouter, HTTPException, Request

from app.config import settings
from app.errors import ConfigError, OWUIError
from app.logging import get_logger
from app.owui import owui_request

logger = get_logger(__name__)

router = APIRouter(prefix="/notes-sync", tags=["notes-sync"])

# OWUI paginates the notes list at a fixed 60 notes/page.
NOTES_PAGE_SIZE = 60


class SyncStatus:
    """Track the last sync state for incremental updates."""

    def __init__(self):
        self.last_sync: str | None = None
        self.last_count: int = 0
        self.last_error: str | None = None
        self.manifest: dict = {}  # filename -> checksum


sync_status = SyncStatus()


def _static_auth() -> dict:
    if not settings.has_static_owui_token():
        raise ConfigError(
            "Notes sync requires OWUI_TOKEN (a static Open-WebUI API key) "
            "— set it in the environment to enable notes sync."
        )
    return {"Authorization": f"Bearer {settings.OWUI_TOKEN}"}


def _request(method: str, path: str, request: Request | None = None, **kwargs):
    """Request helper for the static OWUI_TOKEN (shared client when a
    ``Request`` is in scope, per-call client for background jobs)."""
    return owui_request(
        method,
        path,
        auth_headers=_static_auth(),
        request=request,
        timeout=60.0,
        **kwargs,
    )


def _note_markdown(note: dict) -> str:
    """Markdown body of a note: ``data.content.md`` (the editor's
    export format), falling back to the whole data dict as JSON."""
    data = note.get("data")
    if isinstance(data, dict):
        content = data.get("content")
        if isinstance(content, dict) and isinstance(content.get("md"), str):
            return content["md"]
        if isinstance(content, str):
            return content
    if data:
        return json.dumps(data, indent=2, ensure_ascii=False)
    return ""


def _filename_for_note(title: str, note_id: str) -> str:
    """Filename base (no extension) for a note: title slug, falling back
    to the note id for untitled notes."""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", (title or "").strip())[:80] or note_id
    return slug


async def _fetch_notes(request: Request | None) -> list[dict]:
    """Fetch all notes with full content from Open-WebUI.

    The list response truncates ``data``, so each note is re-fetched
    individually. Any fetch failure aborts the sync *before* any write
    — a partial manifest must never trigger cleanup of KB files.
    """
    summaries: list[dict] = []
    page = 1
    while True:
        batch = None
        for attempt in range(3):
            try:
                batch = await _request(
                    "GET", "/notes", request=request, params={"page": page}
                )
                break
            except OWUIError:
                if attempt == 2:
                    raise
                wait = 2**attempt
                logger.warning(
                    "Notes fetch failed, retrying",
                    page=page,
                    attempt=attempt + 1,
                    wait_seconds=wait,
                )
                await asyncio.sleep(wait)
        if not isinstance(batch, list):
            raise OWUIError("Open-WebUI returned an unexpected /notes response")
        summaries.extend(n for n in batch if isinstance(n, dict) and n.get("id"))
        if len(batch) < NOTES_PAGE_SIZE:
            break
        page += 1

    notes: list[dict] = []
    for summary in summaries:
        full = await _request("GET", f"/notes/{summary['id']}", request=request)
        if not isinstance(full, dict):
            raise OWUIError(
                f"Open-WebUI returned an unexpected note response for {summary['id']}"
            )
        notes.append(full)
    return notes


def _build_manifest(notes: list[dict]) -> list[dict]:
    """Build the sync manifest (filename/path/checksum/size per note).

    A private ``_content`` key carries the note body for the upload
    step; it is stripped before the manifest is sent to the API.
    """
    manifest: list[dict] = []
    used: dict[str, int] = {}
    for note in notes:
        note_id = str(note.get("id") or "unknown")
        base = _filename_for_note(str(note.get("title") or ""), note_id)
        count = used.get(base, 0) + 1
        used[base] = count
        if count > 1:  # duplicate titles: keep filenames unique
            base = f"{base}_{count}"
        filename = f"{base}.md"
        content = _note_markdown(note)
        raw = content.encode("utf-8")
        manifest.append(
            {
                "filename": filename,
                "path": "",
                "checksum": hashlib.sha256(raw).hexdigest(),
                "size": len(raw),
                "_content": content,
            }
        )
    return manifest


async def _upload_file(request: Request | None, filename: str, content: str) -> str:
    """Upload a note as a markdown file; returns the OWUI file id.

    ``process_in_background=false`` so the file is parsed before
    ``/knowledge/{id}/file/add`` requires it to be processed.
    """
    response = await owui_request(
        "POST",
        "/files/",
        auth_headers=_static_auth(),
        request=request,
        timeout=120.0,
        files={"file": (filename, content.encode("utf-8"), "text/markdown")},
        params={"process": "true", "process_in_background": "false"},
    )
    if not isinstance(response, dict) or not response.get("id"):
        raise OWUIError("Open-WebUI file upload did not return a file id")
    return str(response["id"])


async def _sync_to_kb(kb_id: str | None = None, request: Request | None = None) -> dict:
    """Perform an incremental sync of notes to a Knowledge Base."""
    kb_id = kb_id or settings.NOTES_SYNC_KB_ID
    if not kb_id:
        raise HTTPException(
            status_code=400,
            detail="No Knowledge Base ID specified. Set NOTES_SYNC_KB_ID or pass kb_id as a parameter.",
        )
    _static_auth()  # fail early with a clear ConfigError

    logger.info("Starting notes sync", kb_id=kb_id)

    notes = await _fetch_notes(request)
    manifest = _build_manifest(notes)
    results = {
        "total_notes": len(notes),
        "needs_upload": 0,
        "stale_files": 0,
        "uploaded": [],
        "cleaned_up": [],
    }
    if not manifest:
        logger.info("Notes sync: no notes to sync", kb_id=kb_id)
        _finish_sync(kb_id, results, manifest)
        return results

    by_filename = {item["filename"]: item for item in manifest}

    # 1) Ask the server what changed.
    diff = await _request(
        "POST",
        f"/knowledge/{kb_id}/sync/diff",
        request=request,
        json={
            "manifest": [
                {k: item[k] for k in ("filename", "path", "checksum", "size")}
                for item in manifest
            ]
        },
    )
    if not isinstance(diff, dict):
        raise OWUIError("Open-WebUI returned an unexpected sync/diff response")

    to_sync = list(diff.get("added") or []) + list(diff.get("modified") or [])
    stale_file_ids = [
        d.get("file_id") for d in diff.get("deleted") or [] if d.get("file_id")
    ]
    stale_file_ids += [
        m.get("stale_file_id")
        for m in diff.get("modified") or []
        if m.get("stale_file_id")
    ]
    rmdir = list(diff.get("rmdir") or [])
    directory_map = diff.get("directory_map") or {}

    results["needs_upload"] = len(to_sync)
    results["stale_files"] = len(stale_file_ids) + len(rmdir)
    logger.info(
        "Notes sync diff",
        kb_id=kb_id,
        added=len(diff.get("added") or []),
        modified=len(diff.get("modified") or []),
        deleted=len(diff.get("deleted") or []),
        unmodified=diff.get("unmodified_count", 0),
    )

    # 2) Upload + link each changed file.
    for entry in to_sync:
        filename = entry.get("filename")
        item = by_filename.get(filename or "")
        if item is None:
            results.setdefault("errors", []).append(
                {"filename": str(filename), "error": "no matching note found locally"}
            )
            continue
        try:
            file_id = await _upload_file(request, filename, item["_content"])
            await _request(
                "POST",
                f"/knowledge/{kb_id}/file/add",
                request=request,
                json={
                    "file_id": file_id,
                    "directory_id": directory_map.get(entry.get("path") or ""),
                },
            )
            results["uploaded"].append(filename)
            logger.debug("Uploaded note to KB", filename=filename)
        except OWUIError as e:
            logger.error("Failed to upload note to KB", filename=filename, error=str(e))
            results.setdefault("errors", []).append(
                {"filename": str(filename), "error": str(e)}
            )

    # 3) Remove stale files/directories the diff flagged.
    if stale_file_ids or rmdir:
        try:
            await _request(
                "POST",
                f"/knowledge/{kb_id}/sync/cleanup",
                request=request,
                json={"file_ids": stale_file_ids, "dir_ids": rmdir},
            )
            results["cleaned_up"] = stale_file_ids
            logger.info(
                "Cleaned up stale KB files", count=len(stale_file_ids) + len(rmdir)
            )
        except OWUIError as e:
            logger.error("Cleanup failed", error=str(e))
            results.setdefault("errors", []).append(
                {"operation": "cleanup", "error": str(e)}
            )

    _finish_sync(kb_id, results, manifest)
    return results


def _finish_sync(kb_id: str, results: dict, manifest: list) -> None:
    sync_status.last_sync = datetime.now(UTC).isoformat()
    sync_status.last_count = results.get("total_notes", 0)
    sync_status.manifest = {item["filename"]: item["checksum"] for item in manifest}
    sync_status.last_error = next(
        (e.get("error") for e in results.get("errors", []) if e.get("error")),
        None,
    )
    logger.info(
        "Notes sync complete",
        kb_id=kb_id,
        total=results.get("total_notes"),
        uploaded=len(results.get("uploaded", [])),
        cleaned=len(results.get("cleaned_up", [])),
        errors=len(results.get("errors", [])),
    )


@router.post("/", summary="Trigger a notes-to-KB sync", operation_id="NotesSyncTrigger")
async def trigger_sync(request: Request, kb_id: str | None = None) -> dict:
    """
    Manually trigger a sync of all Open-WebUI notes into a Knowledge Base.

    This performs an incremental sync: only new or modified notes are uploaded.
    Runs as the static OWUI_TOKEN.
    """
    try:
        results = await _sync_to_kb(kb_id, request=request)
        return {
            "status": "success",
            "synced_at": sync_status.last_sync,
            **results,
        }
    except HTTPException:
        raise
    except (OWUIError, ConfigError, TypeError, KeyError, ValueError) as e:
        logger.error("Sync failed", error=str(e))
        return {"status": "error", "message": str(e)}


@router.get("/status", summary="Get sync status", operation_id="NotesSyncStatus")
async def get_sync_status() -> dict:
    """Get the current status of the notes sync."""
    return {
        "last_sync": sync_status.last_sync,
        "last_count": sync_status.last_count,
        "last_error": sync_status.last_error,
        "kb_id": settings.NOTES_SYNC_KB_ID or "not configured",
        "auto_sync_enabled": settings.NOTES_SYNC_INTERVAL > 0,
        "sync_interval_seconds": settings.NOTES_SYNC_INTERVAL,
    }


@router.get("/health", summary="Check OWUI connection", operation_id="NotesSyncHealth")
async def owui_health(request: Request) -> dict:
    """Check if Open-WebUI is configured and accessible as the static
    OWUI_TOKEN (notes sync always runs as the static token)."""
    if not settings.is_owui_configured():
        return {"status": "error", "message": "Open-WebUI is not configured"}
    if not settings.has_static_owui_token():
        return {
            "status": "error",
            "message": "OWUI_TOKEN is not set; notes sync runs as the static token.",
        }
    try:
        await _request("GET", "/notes", request=request, params={"page": 1})
        return {
            "status": "ok",
            "instance": settings.OWUI_INSTANCE_URL,
            "notes_accessible": True,
        }
    except OWUIError as e:
        return {"status": "error", "message": str(e)}


@router.get(
    "/debug", summary="Debug notes API endpoints", operation_id="NotesSyncDebug"
)
async def debug_endpoints(request: Request) -> dict:
    """
    Test the notes API endpoints and return their raw responses.

    Use this to diagnose why notes aren't being fetched. Shows status codes,
    response types, and first 500 chars of each response.
    """
    if not settings.is_owui_configured():
        return {"status": "error", "message": "Open-WebUI is not configured"}
    if not settings.has_static_owui_token():
        return {
            "status": "error",
            "message": "OWUI_TOKEN is not set; notes sync runs as the static token.",
        }

    endpoints = [
        "/notes",
        "/notes/search",
    ]

    results = []
    for path in endpoints:
        try:
            url = f"{settings.owui_api_url}{path}"
            headers = {"Authorization": f"Bearer {settings.OWUI_TOKEN}"}
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.request(
                    "GET", url, headers=headers, params={"page": 1}
                )
                results.append(
                    {
                        "endpoint": path,
                        "status_code": response.status_code,
                        "content_type": response.headers.get("content-type", "unknown"),
                        "body_preview": response.text[:500],
                        "body_length": len(response.text),
                    }
                )
        except Exception as e:  # noqa: BLE001 - diagnostic: report any failure kind
            results.append(
                {
                    "endpoint": path,
                    "error": str(e),
                }
            )

    return {
        "owui_url": settings.OWUI_INSTANCE_URL,
        "endpoints_tested": len(results),
        "results": results,
    }
