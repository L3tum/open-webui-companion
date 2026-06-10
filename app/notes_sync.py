"""Notes-to-Knowledge-Base sync for Open-WebUI.

Fetches all notes from Open-WebUI and pushes them into a Knowledge Base
for automatic RAG indexing. Uses incremental sync (SHA-256 checksums)
so only changed notes are uploaded.
"""

import hashlib
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException

from app.config import settings
from app.errors import OWUIError
from app.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/notes-sync", tags=["notes-sync"])


class SyncStatus:
    """Track the last sync state for incremental updates."""

    def __init__(self):
        self.last_sync: Optional[str] = None
        self.last_count: int = 0
        self.last_error: Optional[str] = None
        self.manifest: dict = {}  # filename -> checksum


sync_status = SyncStatus()


async def _owui_request(method: str, path: str, **kwargs) -> dict:
    """Make a request to the Open-WebUI API with error handling."""
    settings.require_owui_config()

    url = f"{settings.owui_api_url}{path}"
    kwargs.setdefault("headers", settings.owui_headers)

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.request(method, url, **kwargs)
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as e:
        error_body = e.response.text[:500]
        logger.error(
            "Open-WebUI API request failed",
            method=method,
            path=path,
            status_code=e.response.status_code,
            response=error_body,
        )
        raise OWUIError(
            f"Open-WebUI API error {e.response.status_code}: {error_body}",
            status_code=e.response.status_code,
        ) from e
    except httpx.RequestError as e:
        logger.error("Open-WebUI API request error", method=method, path=path, error=str(e))
        raise OWUIError(f"Failed to connect to Open-WebUI: {e}") from e


async def _fetch_notes() -> list:
    """Fetch all notes from Open-WebUI.

    Tries multiple known endpoints since the notes API path has changed
    across Open-WebUI versions and may not be publicly documented.
    """
    endpoints = [
        "/api/v1/notes",
        "/api/notes",
        "/api/v1/workspace/notes",
        "/notes",
    ]

    for path in endpoints:
        try:
            response = await _owui_request("GET", path)
            # Handle various response formats
            if isinstance(response, list):
                logger.info("Notes fetched successfully", endpoint=path, count=len(response))
                return response
            if isinstance(response, dict):
                if "results" in response:
                    logger.info("Notes fetched successfully", endpoint=path, count=len(response["results"]))
                    return response["results"]
                if "notes" in response:
                    logger.info("Notes fetched successfully", endpoint=path, count=len(response["notes"]))
                    return response["notes"]
                # Single note or empty dict
                if response:
                    logger.info("Notes fetched successfully", endpoint=path, count=1)
                    return [response]
            logger.debug("Endpoint returned non-note data", endpoint=path, type=type(response).__name__)
        except OWUIError as e:
            logger.debug("Endpoint failed", endpoint=path, error=str(e))
            continue
        except Exception as e:
            logger.debug("Endpoint error", endpoint=path, error=str(e))
            continue

    logger.error("Failed to fetch notes from any known endpoint", endpoints=endpoints)
    raise OWUIError(
        "Failed to fetch notes from any known endpoint. "
        "Open-WebUI's notes API may not be publicly accessible. "
        "Consider exporting notes manually or using the /notes-sync/debug endpoint to troubleshoot."
    )


async def _build_manifest(notes: list) -> list:
    """Build a manifest of note checksums for incremental sync."""
    manifest = []
    for note in notes:
        note_id = note.get("id", note.get("_id", "unknown"))
        content = note.get("content", note.get("text", ""))
        title = note.get("title", f"note_{note_id}")

        # Use SHA-256 of the content as checksum
        checksum = hashlib.sha256(content.encode()).hexdigest()
        size = len(content.encode())

        manifest.append({
            "filename": f"{title}.md",
            "checksum": checksum,
            "content": content,
            "size": size,
            "note_id": note_id,
        })

    return manifest


async def _sync_to_kb(kb_id: Optional[str] = None) -> dict:
    """Perform an incremental sync of notes to a Knowledge Base."""
    kb_id = kb_id or settings.NOTES_SYNC_KB_ID

    if not kb_id:
        raise HTTPException(
            status_code=400,
            detail="No Knowledge Base ID specified. Set NOTES_SYNC_KB_ID or pass kb_id as a parameter.",
        )

    logger.info("Starting notes sync", kb_id=kb_id)

    # Fetch all notes
    notes = await _fetch_notes()
    manifest = await _build_manifest(notes)

    # Determine what needs uploading (changed or new notes)
    needs_upload = []
    for item in manifest:
        old_checksum = sync_status.manifest.get(item["filename"])
        if old_checksum != item["checksum"]:
            needs_upload.append(item)

    # Determine what needs cleanup (deleted notes)
    current_files = set(item["filename"] for item in manifest)
    stale_files = [f for f in sync_status.manifest if f not in current_files]

    results = {
        "total_notes": len(notes),
        "needs_upload": len(needs_upload),
        "stale_files": len(stale_files),
        "uploaded": [],
        "cleaned_up": [],
    }

    # Upload changed/new notes
    if needs_upload:
        try:
            diff_payload = {"manifest": manifest}
            await _owui_request("POST", f"/knowledge/{kb_id}/sync/diff", json=diff_payload)

            for item in needs_upload:
                try:
                    upload_payload = {
                        "filename": item["filename"],
                        "content": item["content"],
                        "size": item["size"],
                    }
                    await _owui_request("POST", f"/knowledge/{kb_id}/sync/upload", json=upload_payload)
                    results["uploaded"].append(item["filename"])
                    logger.debug("Uploaded note", filename=item["filename"])
                except OWUIError as e:
                    logger.error("Failed to upload note", filename=item["filename"], error=str(e))
                    results.setdefault("errors", []).append({
                        "filename": item["filename"],
                        "error": str(e),
                    })

        except OWUIError as e:
            logger.error("Sync diff failed", error=str(e))
            results["error"] = str(e)

    # Clean up stale files
    if stale_files:
        try:
            await _owui_request("POST", f"/knowledge/{kb_id}/sync/cleanup")
            results["cleaned_up"] = stale_files
            logger.info("Cleaned up stale files", count=len(stale_files))
        except OWUIError as e:
            logger.error("Cleanup failed", error=str(e))
            results.setdefault("errors", []).append({"operation": "cleanup", "error": str(e)})

    # Update sync status
    sync_status.last_sync = datetime.now(timezone.utc).isoformat()
    sync_status.last_count = len(notes)
    sync_status.manifest = {item["filename"]: item["checksum"] for item in manifest}
    sync_status.last_error = results.get("error")

    logger.info(
        "Notes sync complete",
        kb_id=kb_id,
        total=results["total_notes"],
        uploaded=results["needs_upload"],
        cleaned=results["stale_files"],
        error=results.get("error"),
    )

    return results


@router.post("/", summary="Trigger a notes-to-KB sync", operation_id="NotesSyncTrigger")
async def trigger_sync(kb_id: Optional[str] = None) -> dict:
    """
    Manually trigger a sync of all Open-WebUI notes into a Knowledge Base.

    This performs an incremental sync: only new or modified notes are uploaded.
    """
    try:
        results = await _sync_to_kb(kb_id)
        return {
            "status": "success",
            "synced_at": sync_status.last_sync,
            **results,
        }
    except HTTPException:
        raise
    except Exception as e:
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
async def owui_health() -> dict:
    """Check if Open-WebUI is configured and accessible."""
    if not settings.is_owui_configured():
        return {"status": "error", "message": "Open-WebUI is not configured"}

    try:
        response = await _owui_request("GET", "/notes", params={"count": 1})
        return {
            "status": "ok",
            "instance": settings.OWUI_INSTANCE_URL,
            "notes_accessible": True,
        }
    except OWUIError as e:
        return {"status": "error", "message": str(e)}


@router.get("/debug", summary="Debug notes API endpoints", operation_id="NotesSyncDebug")
async def debug_endpoints() -> dict:
    """
    Test all known notes API endpoints and return their raw responses.

    Use this to diagnose why notes aren't being fetched. Shows status codes,
    response types, and first 500 chars of each response.
    """
    if not settings.is_owui_configured():
        return {"status": "error", "message": "Open-WebUI is not configured"}

    endpoints = [
        "/api/v1/notes",
        "/api/notes",
        "/api/v1/workspace/notes",
        "/notes",
        "/api/v1/notes/search",
    ]

    results = []
    for path in endpoints:
        try:
            url = f"{settings.owui_api_url}{path}"
            headers = settings.owui_headers
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.request("GET", url, headers=headers)
                results.append({
                    "endpoint": path,
                    "status_code": response.status_code,
                    "content_type": response.headers.get("content-type", "unknown"),
                    "body_preview": response.text[:500],
                    "body_length": len(response.text),
                })
        except Exception as e:
            results.append({
                "endpoint": path,
                "error": str(e),
            })

    return {
        "owui_url": settings.OWUI_INSTANCE_URL,
        "endpoints_tested": len(results),
        "results": results,
    }
