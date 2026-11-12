"""Inventory tools (Homebox-style) backed by SilverBullet notes.

Storage layout (SilverBullet note names, relative to the workspace root):

    inventory/                          # the inventory root (implicit)
    inventory/<location>/               # a location is a *folder note*
    inventory/<location>/<sub>/...      # nested locations
    inventory/<location>/<item>.md      # an item is a regular note

Items carry arbitrary properties (e.g. ``colour``, ``cable length``)
which are written both into the YAML frontmatter (machine-readable) and
into the page body as a table (human-readable). Locations may carry
properties too (e.g. ``shelves: 3``).

The SilverBullet tools are reached over MCP through the
``silverbullet-mcp`` server that Open-WebUI itself uses
(``SILVERBULLET_MCP_URL`` / ``SILVERBULLET_MCP_TOKEN``, see
:mod:`app.config`) — no separate SilverBullet credential needed.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import UTC, datetime
from typing import Any, Literal

import yaml
from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.config import settings
from app.errors import InventoryError, MCPError
from app.logging import get_logger
from app.mcp import MCPClient

logger = get_logger(__name__)

router = APIRouter(prefix="/inventory", tags=["inventory"])

INVENTORY_ROOT = "inventory"
_LIST_PAGE_SIZE = 500  # silverbullet-mcp list-notes max

_client_lock = asyncio.Lock()
_shared_client: MCPClient | None = None
_shared_key: tuple[str, str, float] | None = None


def _reset_shared_client() -> None:
    """Drop the cached MCP client (test seam; also used on config change)."""
    global _shared_client, _shared_key
    _shared_client = None
    _shared_key = None


async def _mcp() -> MCPClient:
    """The shared MCP client, rebuilt when the config changes."""
    global _shared_client, _shared_key
    settings.require_silverbullet_mcp_config()
    key = (
        settings.SILVERBULLET_MCP_URL,
        settings.SILVERBULLET_MCP_TOKEN,
        settings.SILVERBULLET_MCP_TIMEOUT,
    )
    if _shared_client is not None and _shared_key == key:
        return _shared_client
    async with _client_lock:
        if _shared_client is not None:
            if _shared_key == key:
                return _shared_client
            await _shared_client.aclose()
        _shared_client = MCPClient(
            url=settings.SILVERBULLET_MCP_URL,
            token=settings.SILVERBULLET_MCP_TOKEN,
            timeout=settings.SILVERBULLET_MCP_TIMEOUT,
        )
        _shared_key = key
        return _shared_client


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class FindInventoryRequest(BaseModel):
    """Search the inventory for items (default) or locations."""

    query: str | None = Field(
        None,
        description=(
            "Search term matched against item names and note content "
            "(case-insensitive). Omit to list everything."
        ),
    )
    type: Literal["item", "location"] = Field(
        "item",
        description="What to look for: 'item' (default) or 'location'.",
    )
    location: str | None = Field(
        None,
        description=(
            "Restrict results to this location, a path relative to "
            "inventory/ (e.g. 'office' or 'office/drawer')."
        ),
    )
    limit: int = Field(
        50, ge=1, le=200, description="Maximum number of results to return."
    )
    details: bool = Field(
        False,
        description="Also fetch the properties (frontmatter) of the returned items.",
    )


class AddInventoryRequest(BaseModel):
    """Add an item, or create a location (``type='location'``)."""

    type: Literal["item", "location"] = Field(
        "item",
        description="'item' (default) or 'location'.",
    )
    name: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description=(
            "For items: the item name (e.g. 'USB-C Cable'). "
            "For locations: the path relative to inventory/ (e.g. 'office/drawer')."
        ),
    )
    location: str | None = Field(
        None,
        description=(
            "Location for the item: a full path relative to inventory/ or a "
            "unique leaf name. Ambiguous names are rejected with the candidates."
        ),
    )
    properties: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Arbitrary properties (e.g. {'colour': 'red', 'cable length': '5 m'}); "
            "written to both frontmatter and page body."
        ),
    )
    description: str | None = Field(
        None,
        description="Optional free-text description stored in the page body.",
    )
    overwrite: bool = Field(
        False,
        description="Replace an existing item with the same name in the same location.",
    )


class TakeInventoryRequest(BaseModel):
    """Mark an item as taken in its frontmatter (nothing is deleted)."""

    type: Literal["item", "location"] = Field(
        "item",
        description="'item' (default). 'location' is rejected: nothing is deleted anymore.",
    )
    name: str = Field(
        ...,
        min_length=1,
        max_length=300,
        description=(
            "Item name, or a full item path relative to inventory/ "
            "(e.g. 'usb-c-cable' or 'office/drawer/usb-c-cable')."
        ),
    )
    location: str | None = Field(
        None,
        description="Disambiguate an item name by location (path or unique leaf name).",
    )


class InventoryEntry(BaseModel):
    """One item or location in the inventory."""

    path: str = Field(
        ...,
        description="SilverBullet note name: items end in '.md', locations in '/'.",
    )
    name: str = Field(..., description="Basename of the note (without extension).")
    location: str = Field(
        ...,
        description="Containing location relative to inventory/ ('' = inventory root).",
    )
    properties: dict[str, str] = Field(
        default_factory=dict,
        description="Frontmatter properties (requires details=true for items).",
    )
    snippet: str | None = Field(
        None, description="Content snippet (query matches only)."
    )


class FindInventoryResponse(BaseModel):
    type: Literal["item", "location"]
    count: int
    truncated: bool = Field(
        ..., description="True when more matches exist than were returned."
    )
    results: list[InventoryEntry]


class AddInventoryResponse(BaseModel):
    created: bool = Field(
        ..., description="False when the note already existed (idempotent)."
    )
    type: Literal["item", "location"]
    path: str
    location: str | None = None
    properties: dict[str, str] = Field(default_factory=dict)


class TakeInventoryResponse(BaseModel):
    taken: bool = True
    type: Literal["item"]
    path: str
    name: str
    taken_at: str | None = Field(
        None,
        description="ISO-8601 (UTC) timestamp written to the item's frontmatter.",
    )
    properties: dict[str, str] = Field(
        default_factory=dict,
        description="Resulting frontmatter properties (includes the taken markers).",
    )


# ---------------------------------------------------------------------------
# Name / path helpers
# ---------------------------------------------------------------------------

_UNSAFE_SEGMENT = re.compile(r'[*?"<>|]')


def _sanitize_segment(raw: str) -> str:
    seg = raw.replace("\\", "/").strip()
    seg = _UNSAFE_SEGMENT.sub("-", seg)
    seg = re.sub(r"\s+", " ", seg).strip().strip(".")
    return seg


def _sanitize_item_name(raw: str) -> str:
    """Sanitize an item name into a note basename (no '/', no '.md')."""
    seg = _sanitize_segment(raw.replace("/", "-"))
    if seg.lower().endswith(".md"):
        seg = seg[: -len(".md")]
    return seg[:120].strip()


def _normalize_location(raw: str | None) -> str:
    """Normalize a location to a relative path (no 'inventory/' prefix, no slashes at the edges)."""
    if not raw:
        return ""
    loc = raw.strip()
    if loc.lower().startswith(INVENTORY_ROOT + "/"):
        loc = loc[len(INVENTORY_ROOT) + 1 :]
    loc = loc.strip("/").strip()
    if loc.lower().endswith(".md"):
        loc = loc[: -len(".md")]
    segments = [
        s for s in (_sanitize_segment(p) for p in loc.split("/")) if s and s != "."
    ]
    return "/".join(segments)


def _note_stem(name: str) -> str:
    """'inventory/a/b.md' -> 'inventory/a/b'; 'inventory/a/' -> 'inventory/a'."""
    n = name
    if n.endswith("/"):
        return n.rstrip("/")
    if n.lower().endswith(".md"):
        return n[: -len(".md")]
    return n


def _is_folder(name: str) -> bool:
    return name.endswith("/")


def _relative(stem: str) -> str:
    """Note stem -> path relative to inventory/ ('' for the root itself)."""
    if stem == INVENTORY_ROOT:
        return ""
    prefix = INVENTORY_ROOT + "/"
    if stem.startswith(prefix):
        return stem[len(prefix) :]
    return stem


def _item_location(stem: str) -> str:
    rel = _relative(stem)
    return rel.rsplit("/", 1)[0] if "/" in rel else ""


def _item_name(stem: str) -> str:
    return _relative(stem).rsplit("/", 1)[-1]


def _as_filename(name: str) -> str:
    """Canonical MCP filename: folders keep '/', docs get '.md'."""
    if name.endswith("/"):
        return name
    return name if name.endswith(".md") else name + ".md"


def _is_conflict_message(message: str) -> bool:
    return bool(re.search(r"exist|already|conflict", message, re.IGNORECASE))


# ---------------------------------------------------------------------------
# MCP tool wrappers (defensive about response shapes)
# ---------------------------------------------------------------------------


def _as_name_list(result: Any) -> list[str]:
    if isinstance(result, list):
        entries = result
    elif isinstance(result, dict):
        entries = next(
            (
                result[k]
                for k in ("notes", "items", "results", "names")
                if isinstance(result.get(k), list)
            ),
            [],
        )
    else:
        entries = []
    names: list[str] = []
    for entry in entries:
        if isinstance(entry, str):
            names.append(entry)
        elif isinstance(entry, dict):
            for key in ("filename", "name", "path", "note"):
                val = entry.get(key)
                if isinstance(val, str) and val:
                    names.append(val)
                    break
    return names


def _next_cursor(result: Any) -> str | None:
    if isinstance(result, dict):
        for key in ("nextCursor", "cursor", "next_cursor"):
            val = result.get(key)
            if isinstance(val, str) and val:
                return val
    return None


async def _list_notes() -> tuple[list[str], bool]:
    """All note names under inventory/ (folders and items), paginated."""
    mcp = await _mcp()
    names: list[str] = []
    seen: set[str] = set()
    cursor: str | None = None
    truncated = False
    for _ in range(max(1, settings.INVENTORY_MAX_LIST_PAGES)):
        args: dict[str, Any] = {"limit": _LIST_PAGE_SIZE}
        if cursor:
            args["cursor"] = cursor
        result = await mcp.call_tool("list-notes", args)
        batch = _as_name_list(result)
        new = [n for n in batch if n.startswith(INVENTORY_ROOT) and n not in seen]
        seen.update(new)
        names.extend(new)
        cursor = _next_cursor(result)
        if not cursor:
            break
    else:
        truncated = True
    return names, truncated


async def _read_note(filename: str) -> str:
    result = await (await _mcp()).call_tool("read-note", {"filename": filename})
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        for key in ("content", "markdown", "text"):
            val = result.get(key)
            if isinstance(val, str):
                return val
    raise MCPError(f"Could not extract note content for {filename}")


def _search_hits(result: Any) -> list[tuple[str, str | None]]:
    """(note name, snippet) pairs out of a search-notes result."""
    if isinstance(result, list):
        entries = result
    elif isinstance(result, dict):
        entries = next(
            (
                result[k]
                for k in ("results", "items", "matches", "notes", "hits")
                if isinstance(result.get(k), list)
            ),
            [],
        )
    else:
        entries = []
    hits: list[tuple[str, str | None]] = []
    for entry in entries:
        filename: str | None = None
        snippet: str | None = None
        if isinstance(entry, str):
            filename = entry
        elif isinstance(entry, dict):
            filename = next(
                (
                    entry[k]
                    for k in ("filename", "name", "path", "note")
                    if isinstance(entry.get(k), str) and entry.get(k)
                ),
                None,
            )
            snippet = (
                entry.get("snippet") or entry.get("preview") or entry.get("excerpt")
            )
            if not isinstance(snippet, str):
                matches = entry.get("matches")
                if isinstance(matches, list) and matches:
                    first = matches[0]
                    snippet = (
                        first
                        if isinstance(first, str)
                        else (first.get("line") if isinstance(first, dict) else None)
                    )
        if not isinstance(filename, str) or not filename:
            continue
        if not filename.startswith(INVENTORY_ROOT) or filename.endswith("/"):
            continue
        hits.append((filename, snippet if isinstance(snippet, str) else None))
    return hits


async def _search_items(query: str) -> list[tuple[str, str | None]]:
    """Full-text search restricted to inventory items (folders excluded)."""
    result = await (await _mcp()).call_tool(
        "search-notes",
        {
            "query": query,
            "searchType": "both",
            "caseSensitive": False,
            "useRegex": False,
            "maxResults": 100,
            "concise": True,
            "enableCaching": False,
        },
    )
    return _search_hits(result)


def _parse_frontmatter(content: str) -> dict[str, Any]:
    """Parse the YAML frontmatter block of a note ({} when absent/broken)."""
    if not content.lstrip().startswith("---"):
        return {}
    stripped = content.lstrip()
    body = stripped[3:]
    end = body.find("\n---")
    if end == -1:
        return {}
    block = body[:end].strip()
    try:
        data = yaml.safe_load(block)
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if v is not None}
    except yaml.YAMLError:
        logger.warning("Frontmatter is not valid YAML; falling back to line parsing")
    fallback: dict[str, Any] = {}
    for line in block.splitlines():
        if not line or line.startswith(("#", " ", "\t")) or ":" not in line:
            continue
        key, _, value = line.partition(":")
        fallback[key.strip()] = value.strip().strip("\"'")
    return fallback


def _public_properties(props: dict[str, Any]) -> dict[str, str]:
    return {
        k: v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
        for k, v in props.items()
        if k not in ("type", "title", "location") and v is not None
    }


def _md_cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


# ---------------------------------------------------------------------------
# Location resolution
# ---------------------------------------------------------------------------


async def _resolve_location(raw: str, folders: list[str]) -> str:
    """Resolve a user-supplied location to an existing folder.

    Matching order: exact path, unique leaf name, unique path suffix,
    unique substring. Raises :class:`InventoryError` (409 with
    candidates, 404 with a create hint).
    """
    target = _normalize_location(raw)
    if not target:
        raise InventoryError(f"Location '{raw}' is not a usable location path", 400)
    rel_folders = sorted({_relative(_note_stem(f)) for f in folders} - {""})
    t = target.lower()

    for match in (
        [r for r in rel_folders if r.lower() == t],
        [r for r in rel_folders if r.rsplit("/", 1)[-1].lower() == t],
        [r for r in rel_folders if r.lower().endswith("/" + t)],
        [r for r in rel_folders if t in r.lower()],
    ):
        if len(match) == 1:
            return match[0]
        if len(match) > 1:
            raise InventoryError(
                f"Location '{raw}' is ambiguous",
                409,
                extra={
                    "candidates": match,
                    "hint": "Pass a more specific location path.",
                },
            )
    raise InventoryError(
        f"Location '{raw}' does not exist. Create it first with "
        f"AddInventory(type='location', name='{target}').",
        404,
        extra={"candidates": [], "hint": "No location matched."},
    )


# ---------------------------------------------------------------------------
# Note generation
# ---------------------------------------------------------------------------


def _yaml_scalar(value: str) -> str:
    v = str(value)
    if not v or v != v.strip() or re.search(r"[:#\[\]{}&*!|>'\"%@`,]", v):
        return json.dumps(v, ensure_ascii=False)
    return v


def _build_note_content(
    kind: str,
    title: str,
    location_rel: str,
    properties: dict[str, str] | None,
    description: str | None,
) -> str:
    """Frontmatter (machine-readable) + body table (human-readable)."""
    props = properties or {}
    lines = [
        "---",
        f"type: {kind}",
        f"title: {_yaml_scalar(title)}",
        f"location: {_yaml_scalar(location_rel or INVENTORY_ROOT)}",
    ]
    for key, value in props.items():
        lines.append(f"{_yaml_scalar(str(key))}: {_yaml_scalar(str(value))}")
    lines += [
        "---",
        "",
        f"# {title}",
        "",
        f"**Location:** `{location_rel or INVENTORY_ROOT}`",
        "",
    ]
    if props:
        lines += ["| Property | Value |", "| --- | --- |"]
        lines += [f"| {_md_cell(k)} | {_md_cell(v)} |" for k, v in props.items()]
        lines.append("")
    if description and description.strip():
        lines += [description.strip(), ""]
    return "\n".join(lines)


def _mark_note_taken(content: str, taken_at: str) -> str:
    """Set ``taken: true`` / ``taken_at`` in a note's YAML frontmatter.

    Surgical line edit: existing lines for those keys are replaced in
    place, missing keys are inserted before the closing ``---``, and a
    minimal frontmatter block is created when the note has none. The
    rest of the note is preserved byte-for-byte.
    """
    lines = content.splitlines()
    trailing_nl = content.endswith("\n")
    start = end = None
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                start, end = 0, i
                break
    # Quoted via _yaml_scalar so YAML keeps the timestamp a plain string
    # (an unquoted ISO-8601 value would be loaded back as a datetime).
    taken_at_yaml = _yaml_scalar(taken_at)
    if start is None:
        lines = ["---", "taken: true", f"taken_at: {taken_at_yaml}", "---", ""] + lines
    else:
        for i in range(start + 1, end):
            key = lines[i].split(":", 1)[0].strip().lower()
            if key == "taken":
                lines[i] = "taken: true"
            elif key == "taken_at":
                lines[i] = f"taken_at: {taken_at_yaml}"
        keys = {
            lines[i].split(":", 1)[0].strip().lower() for i in range(start + 1, end)
        }
        if "taken" not in keys:
            lines.insert(end, "taken: true")
            end += 1
        if "taken_at" not in keys:
            lines.insert(end, f"taken_at: {taken_at_yaml}")
    text = "\n".join(lines)
    return text + ("\n" if trailing_nl else "")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


async def _entry_for_item(
    name: str, details: bool, snippet: str | None = None
) -> InventoryEntry:
    stem = _note_stem(name)
    entry = InventoryEntry(
        path=_as_filename(stem),
        name=_item_name(stem),
        location=_item_location(stem),
        snippet=snippet,
    )
    if details:
        try:
            content = await _read_note(_as_filename(stem))
            entry.properties = _public_properties(_parse_frontmatter(content))
        except MCPError:
            logger.warning("Could not read note for details", path=name)
    return entry


@router.post(
    "/find",
    summary="Find items or locations in the inventory",
    operation_id="FindInventory",
    response_model=FindInventoryResponse,
)
async def find_inventory(request: FindInventoryRequest) -> FindInventoryResponse:
    """Search or list the inventory.

    Default (``type='item'``): lists/searches item notes. With
    ``type='location'`` it lists locations (folder notes). A ``query``
    triggers a full-text search over names and content; without one
    everything (up to ``limit``) is listed. ``location`` restricts
    results to a location subtree.
    """
    settings.require_silverbullet_mcp_config()
    names, list_truncated = await _list_notes()
    folders = [n for n in names if _is_folder(n)]
    items = [n for n in names if not _is_folder(n)]
    query = (request.query or "").strip()
    location_rel = _normalize_location(request.location)
    truncated = list_truncated

    if request.type == "location":
        candidates = sorted(
            {_relative(_note_stem(f)) for f in folders} - {""}
        )  # drop the inventory/ root itself
        if location_rel:
            candidates = [
                c
                for c in candidates
                if c == location_rel or c.startswith(location_rel + "/")
            ]
        if query:
            candidates = [c for c in candidates if query.lower() in c.lower()]
        truncated = truncated or len(candidates) > request.limit
        ordered = candidates[: request.limit]
        results = [
            InventoryEntry(
                path=f"inventory/{c}/",
                name=c.rsplit("/", 1)[-1],
                location=c.rsplit("/", 1)[0] if "/" in c else "",
            )
            for c in ordered
        ]
        return FindInventoryResponse(
            type="location", count=len(results), truncated=truncated, results=results
        )

    if query:
        try:
            hits = await _search_items(query)
        except MCPError:
            logger.warning("search-notes failed; falling back to name matching only")
            hits = []
        if location_rel:
            hits = [
                (n, s)
                for n, s in hits
                if (loc := _item_location(_note_stem(n)))
                in (location_rel, location_rel + "/")
                or loc.startswith(location_rel + "/")
            ]
        path_hits = [
            n for n in items if query.lower() in _relative(_note_stem(n)).lower()
        ]
        ordered: list[str] = []
        seen: set[str] = set()
        for n, _snippet in hits:
            if n not in seen:
                seen.add(n)
                ordered.append(n)
        for n in path_hits:
            if n not in seen:
                seen.add(n)
                ordered.append(n)
        snippets = dict(hits)
        truncated = truncated or len(ordered) > request.limit
        ordered = ordered[: request.limit]
        results = [
            await _entry_for_item(n, request.details, snippets.get(n)) for n in ordered
        ]
    else:
        ordered = sorted(items)
        if location_rel:
            ordered = [
                n
                for n in ordered
                if _item_location(_note_stem(n)) in (location_rel, location_rel + "/")
                or _item_location(_note_stem(n)).startswith(location_rel + "/")
            ]
        truncated = truncated or len(ordered) > request.limit
        ordered = ordered[: request.limit]
        results = [await _entry_for_item(n, request.details) for n in ordered]

    return FindInventoryResponse(
        type="item", count=len(results), truncated=truncated, results=results
    )


@router.post(
    "/add",
    summary="Add an item or create a location",
    operation_id="AddInventory",
    response_model=AddInventoryResponse,
)
async def add_inventory(request: AddInventoryRequest) -> AddInventoryResponse:
    """Add an item (``type='item'``, default) or a location
    (``type='location'``).

    For items: the location is resolved against existing folders —
    a full path, or a unique leaf/suffix/substring. Ambiguous
    locations are rejected with the candidates (HTTP 409); missing
    ones with a hint to create them first (HTTP 404).
    """
    settings.require_silverbullet_mcp_config()
    names, _ = await _list_notes()
    folders = [n for n in names if _is_folder(n)]
    items = {n for n in names if not _is_folder(n)}
    props = {
        k: str(v).strip()
        for k, v in (request.properties or {}).items()
        if str(v).strip()
    }

    if request.type == "location":
        target = _normalize_location(request.name)
        if not target:
            raise InventoryError(f"'{request.name}' is not a usable location path", 400)
        path = f"inventory/{target}/"
        existing = next(
            (f for f in folders if _relative(_note_stem(f)).lower() == target.lower()),
            None,
        )
        if existing:
            return AddInventoryResponse(
                created=False, type="location", path=existing, location=""
            )
        mcp = await _mcp()
        content = _build_note_content(
            "location", target.rsplit("/", 1)[-1], target, props, request.description
        )
        try:
            await mcp.call_tool(
                "create-note",
                {"filename": path, "content": content, "overwrite": False},
            )
        except MCPError as exc:
            if _is_conflict_message(str(exc)):
                return AddInventoryResponse(
                    created=False, type="location", path=path, location=""
                )
            raise
        return AddInventoryResponse(
            created=True, type="location", path=path, location="", properties=props
        )

    # --- items -------------------------------------------------------------
    name = _sanitize_item_name(request.name)
    if not name:
        raise InventoryError(f"'{request.name}' is not a usable item name", 400)
    if not request.location:
        raise InventoryError(
            "A location is required for items. Find one with "
            "FindInventory(type='location'), or create one with "
            "AddInventory(type='location').",
            400,
        )
    loc_rel = await _resolve_location(request.location, folders)
    path = f"inventory/{loc_rel}/{name}.md"
    if path in items and not request.overwrite:
        raise InventoryError(
            f"Item '{name}' already exists in '{loc_rel}' "
            f"(inventory/{loc_rel}/{name}.md). Pass overwrite=true to replace it.",
            409,
            extra={"path": path},
        )
    mcp = await _mcp()
    content = _build_note_content("item", name, loc_rel, props, request.description)
    try:
        await mcp.call_tool(
            "create-note",
            {"filename": path, "content": content, "overwrite": request.overwrite},
        )
    except MCPError as exc:
        if _is_conflict_message(str(exc)):
            raise InventoryError(
                f"Could not create '{path}' (note may already exist)",
                409,
                extra={"path": path},
            ) from exc
        raise
    return AddInventoryResponse(
        created=True, type="item", path=path, location=loc_rel, properties=props
    )


@router.post(
    "/take",
    summary="Mark an item as taken in its frontmatter",
    operation_id="TakeInventory",
    response_model=TakeInventoryResponse,
)
async def take_inventory(request: TakeInventoryRequest) -> TakeInventoryResponse:
    """Mark an item as taken — nothing is deleted.

    The item's frontmatter gets ``taken: true`` plus a ``taken_at``
    timestamp (UTC, ISO-8601); the note and its content stay in place.
    Taking the same item twice just refreshes the timestamp.
    """
    settings.require_silverbullet_mcp_config()

    if request.type == "location":
        raise InventoryError(
            "TakeInventory no longer removes anything: items are only "
            "marked as taken in their frontmatter. Locations are folders "
            "and cannot be taken — remove them manually in SilverBullet "
            "if you want them gone.",
            400,
        )
    names, _ = await _list_notes()
    folders = [n for n in names if _is_folder(n)]
    items = [n for n in names if not _is_folder(n)]

    # --- items -------------------------------------------------------------
    raw = request.name.strip()
    if raw.lower().startswith(INVENTORY_ROOT + "/") or "/" in raw:
        stem = _note_stem(raw)
        candidates = [n for n in items if _note_stem(n).lower() == stem.lower()]
    else:
        base = _sanitize_item_name(raw)
        if not base:
            raise InventoryError(f"'{raw}' is not a usable item name", 400)
        if request.location:
            loc_rel = await _resolve_location(request.location, folders)
            candidates = [
                n
                for n in items
                if _note_stem(n).rsplit("/", 1)[-1].lower() == base.lower()
                and _item_location(_note_stem(n)) == loc_rel
            ]
        else:
            candidates = [
                n
                for n in items
                if _note_stem(n).rsplit("/", 1)[-1].lower() == base.lower()
            ]
    if not candidates:
        raise InventoryError(f"No inventory item matching '{raw}' was found", 404)
    if len(candidates) > 1:
        raise InventoryError(
            f"Item name '{raw}' is ambiguous",
            409,
            extra={
                "candidates": [_relative(_note_stem(n)) for n in sorted(candidates)],
                "hint": "Pass the full item path or a location.",
            },
        )
    stem = _note_stem(candidates[0])
    filename = _as_filename(stem)
    try:
        content = await _read_note(filename)
    except MCPError as exc:
        raise InventoryError(
            f"Could not read '{filename}' to mark it as taken "
            "(note may have disappeared)",
            exc.status_code,
        ) from exc
    taken_at = datetime.now(UTC).isoformat(timespec="seconds")
    new_content = _mark_note_taken(content, taken_at)
    try:
        await (await _mcp()).call_tool(
            "create-note",
            {"filename": filename, "content": new_content, "overwrite": True},
        )
    except MCPError as exc:
        raise InventoryError(
            f"Could not write the taken marker to '{filename}'",
            exc.status_code,
        ) from exc
    return TakeInventoryResponse(
        taken=True,
        type="item",
        path=filename,
        name=_item_name(stem),
        taken_at=taken_at,
        properties=_public_properties(_parse_frontmatter(new_content)),
    )


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@router.get(
    "/health",
    summary="Inventory tooling health (MCP connectivity + inventory/ listing)",
    operation_id="InventoryHealth",
)
async def inventory_health() -> dict[str, Any]:
    if not settings.SILVERBULLET_MCP_URL:
        return {
            "status": "degraded",
            "mcp_configured": False,
            "note": "SILVERBULLET_MCP_URL is not set",
        }
    started = time.monotonic()
    try:
        names, truncated = await _list_notes()
    except MCPError as exc:
        return {
            "status": "error",
            "mcp_configured": True,
            "error": str(exc),
            "elapsed_ms": round((time.monotonic() - started) * 1000),
        }
    folders = {f.rstrip("/") for f in names if _is_folder(f)}
    return {
        "status": "ok",
        "mcp_configured": True,
        "inventory": {
            "locations": sorted(
                f[len(INVENTORY_ROOT) + 1 :] for f in folders if f != INVENTORY_ROOT
            ),
            "item_count": sum(1 for n in names if not _is_folder(n)),
            "truncated": truncated,
        },
        "elapsed_ms": round((time.monotonic() - started) * 1000),
    }
