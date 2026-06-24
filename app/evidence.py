"""Evidence management endpoints for Open-WebUI.

Provides persistent storage for evidence items to reduce hallucinations.
Evidence is stored in a JSON file and can be filtered by tags.
"""

import json
import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.config import settings
from app.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/evidence", tags=["evidence"])


class EvidenceItem(BaseModel):
    """Schema for adding new evidence."""

    content: str = Field(..., description="The factual content to store")
    source: str = Field(default="", description="Where this evidence came from")
    tags: list[str] = Field(default_factory=list, description="Tags for categorization")


def _load_evidence() -> list:
    """Load evidence from the JSON file."""
    if not os.path.exists(settings.EVIDENCE_FILE):
        return []

    try:
        with open(settings.EVIDENCE_FILE, "r") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        logger.error("Failed to parse evidence file", error=str(e))
        return []
    except OSError as e:
        logger.error("Failed to read evidence file", error=str(e))
        return []


def _save_evidence(data: list) -> None:
    """Save evidence to the JSON file."""
    try:
        os.makedirs(os.path.dirname(settings.EVIDENCE_FILE), exist_ok=True)
        with open(settings.EVIDENCE_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except OSError as e:
        logger.error("Failed to write evidence file", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to save evidence data")


@router.post("/", summary="Add a new piece of evidence", operation_id="EvidenceAdd")
async def add_evidence(item: EvidenceItem) -> dict:
    """
    Add a new piece of evidence to the persistent database.

    Use this to store verified facts, sources, or findings that should be
    referenced in future conversations to reduce hallucinations.
    """
    data = _load_evidence()
    new_id = len(data) + 1

    entry = {
        "id": new_id,
        "content": item.content,
        "source": item.source,
        "tags": item.tags,
        "added_at": datetime.now(timezone.utc).isoformat(),
    }
    data.append(entry)
    _save_evidence(data)

    logger.info("Evidence added", evidence_id=new_id, tags=item.tags)

    return {
        "status": "success",
        "id": new_id,
        "message": f"Added evidence #{new_id}",
    }


@router.get("/", summary="List all stored evidence", operation_id="EvidenceList")
async def list_evidence(
    tag: Optional[str] = Query(default="", description="Optional tag to filter by"),
    limit: Optional[int] = Query(
        default=50, ge=1, le=200, description="Maximum items to return"
    ),
) -> list:
    """
    Retrieve a list of all stored evidence items.

    Optionally filter by tag. Returns the most recent items first.
    """
    data = _load_evidence()

    if tag:
        data = [e for e in data if tag in e.get("tags", [])]

    # Sort by id descending (most recent first)
    data = sorted(data, key=lambda x: x.get("id", 0), reverse=True)

    logger.debug("Evidence listed", filter_tag=tag, total=len(data), limit=limit)
    return data[:limit]


@router.get(
    "/{evidence_id}", summary="Get a specific evidence item", operation_id="EvidenceGet"
)
async def get_evidence(evidence_id: int) -> dict:
    """Retrieve a specific evidence item by its ID."""
    data = _load_evidence()
    for item in data:
        if item.get("id") == evidence_id:
            logger.debug("Evidence retrieved", evidence_id=evidence_id)
            return item

    raise HTTPException(status_code=404, detail=f"Evidence #{evidence_id} not found")


@router.put(
    "/{evidence_id}", summary="Update an evidence item", operation_id="EvidenceUpdate"
)
async def update_evidence(evidence_id: int, item: EvidenceItem) -> dict:
    """Update an existing evidence item."""
    data = _load_evidence()
    for i, entry in enumerate(data):
        if entry.get("id") == evidence_id:
            data[i] = {
                "id": evidence_id,
                "content": item.content,
                "source": item.source,
                "tags": item.tags,
                "added_at": entry.get(
                    "added_at", datetime.now(timezone.utc).isoformat()
                ),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            _save_evidence(data)
            logger.info("Evidence updated", evidence_id=evidence_id)
            return {"status": "success", "message": f"Updated evidence #{evidence_id}"}

    raise HTTPException(status_code=404, detail=f"Evidence #{evidence_id} not found")


@router.delete(
    "/{evidence_id}", summary="Delete an evidence item", operation_id="EvidenceDelete"
)
async def delete_evidence(evidence_id: int) -> dict:
    """Delete an evidence item by its ID."""
    data = _load_evidence()
    original_len = len(data)
    data = [e for e in data if e.get("id") != evidence_id]

    if len(data) == original_len:
        raise HTTPException(
            status_code=404, detail=f"Evidence #{evidence_id} not found"
        )

    _save_evidence(data)
    logger.info("Evidence deleted", evidence_id=evidence_id)
    return {"status": "success", "message": f"Deleted evidence #{evidence_id}"}


@router.delete("/", summary="Clear all evidence", operation_id="EvidenceClear")
async def clear_evidence() -> dict:
    """Delete all stored evidence items. Use with caution."""
    data = _load_evidence()
    count = len(data)
    _save_evidence([])
    logger.warning("All evidence cleared", count=count)
    return {"status": "success", "message": f"Cleared {count} evidence items"}
