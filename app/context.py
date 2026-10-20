"""Open-WebUI context compaction + recall endpoints.

Built against the Open-WebUI **v0.12.3** contract:

- ``GET /chats/{id}`` returns a ``ChatResponse``: the message history
  lives at ``chat["chat"]["history"]["messages"]`` (a map of message id
  → message; the active branch is the ``parentId`` chain ending at
  ``current_message_id`` / ``history.currentId``), and the model
  context window is reported in the server-computed
  ``context_usage`` dict (``tokens`` / ``threshold`` / ``percent`` /
  ``source``), or is absent when context compaction is disabled.
- ``POST /chats/{id}/compact`` takes an optional ``{"model": ...}``
  body and returns ``compacted`` / ``reason`` /
  ``dropped_messages`` / ``kept_messages`` / ``summary_chars``.
  Compaction is non-destructive: it writes a ``contextSummary``
  checkpoint; the stored messages remain searchable.
- ``GET /chats/search`` is the owner-scoped, server-side, paginated
  (60/page) search used for cross-chat recall.

All Open-WebUI API calls act as the validated calling user (see
``app/identity.py``). With ``OWUI_ALLOW_USER_TOKEN`` enabled (default)
the chat-data endpoints (usage/compact/search) require a forwarded user
token and never fall back to the static ``OWUI_TOKEN``; only the
diagnostic ``/context/health`` endpoint may fall back.

Error shape (tool-oriented): when a request for a *specific chat* fails
against the OWUI backend, the endpoint returns HTTP 200 with
``{"chat_id": ..., "error": "..."}`` so the calling model can read the
problem; validation problems are 4xx, missing configuration is 500,
and an unreachable backend during *identity validation* is a 502.
"""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Query, Request

from app.config import settings
from app.errors import ConfigError, OWUIError
from app.identity import (
    OwuiIdentity,
    resolve_forwarded_identity,
    resolve_identity,
)
from app.logging import get_logger
from app.owui import owui_request

logger = get_logger(__name__)

router = APIRouter(prefix="/context", tags=["context"])

# Set on the OWUI side (ENABLE_FORWARD_USER_INFO_HEADERS=true); names the
# chat the tool call was made from. Advisory — never trusted for auth.
CHAT_ID_HEADER = "X-OpenWebUI-Chat-Id"

# OWUI's server-side chat search is paginated at a fixed 60 chats/page.
OWUI_SEARCH_PAGE_SIZE = 60


def _validated_chat_id(chat_id: str) -> str:
    """Ensure chat_id has the shape OWUI uses (UUID) before it is
    interpolated into an outgoing URL path (injection guard)."""
    try:
        uuid.UUID(chat_id)
    except ValueError:
        raise HTTPException(400, "chat_id must be a valid UUID") from None
    return chat_id


def _log_tool(endpoint: str, identity: OwuiIdentity) -> None:
    logger.info(
        "context tool",
        endpoint=endpoint,
        email=identity.email,
        role=identity.role,
        user_id=identity.user_id,
        source=identity.source,
    )


async def _resolve_chat_data_identity(request: Request) -> OwuiIdentity:
    """Identity for the chat-data endpoints (fail-closed on the static
    token); legacy mode (OWUI_ALLOW_USER_TOKEN=false) uses the static
    token for everything."""
    if settings.OWUI_ALLOW_USER_TOKEN:
        return await resolve_forwarded_identity(request)
    return await resolve_identity(request)


async def _owui_request(
    method: str,
    path: str,
    request: Request,
    identity: OwuiIdentity,
    **kwargs,
) -> dict:
    """Make a request to the Open-WebUI API as the acting identity."""
    return await owui_request(
        method,
        path,
        auth_headers=identity.auth_header,
        request=request,
        **kwargs,
    )


def _resolve_chat_id(request: Request, chat_id: str | None) -> str | None:
    """chat_id from the query parameter, else the advisory
    X-OpenWebUI-Chat-Id header (validated wherever it is used)."""
    return chat_id or request.headers.get(CHAT_ID_HEADER)


def _branch_messages(chat: dict) -> list[dict]:
    """Reconstruct the current message branch of a v0.12.3 chat.

    Current shape: ``chat["chat"]["history"]["messages"]`` is a map of
    message id → message and the active branch is the ``parentId``
    chain ending at the current message. Legacy shape:
    ``chat["chat"]["messages"]`` is a plain list.
    """
    chat_data = chat.get("chat")
    if not isinstance(chat_data, dict):
        return []
    history = chat_data.get("history")
    if isinstance(history, dict):
        messages_map = history.get("messages")
        if isinstance(messages_map, dict):
            current_id = (
                chat.get("current_message_id")
                or history.get("currentId")
                or chat_data.get("currentId")
                or chat_data.get("branchPointMessageId")
            )
            if not current_id:
                return []
            branch: list[dict] = []
            seen: set[str] = set()
            message_id = current_id
            while message_id and message_id not in seen:
                seen.add(message_id)
                message = messages_map.get(message_id)
                if not isinstance(message, dict):
                    break
                branch.append(message)
                message_id = message.get("parentId")
            branch.reverse()
            return branch
    legacy = chat_data.get("messages")
    if isinstance(legacy, list):
        return [m for m in legacy if isinstance(m, dict)]
    return []


def _extract_text(message: dict) -> str:
    """Extract plain text from an Open-WebUI message.

    Handles ``content`` as a string, as a list of parts (strings or
    dicts with a ``text`` key), or as a bare dict; assistant messages
    carry their text in ``output`` parts of type ``message``; the
    legacy ``generation`` field is a last resort.
    """
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        text = content.get("text")
        if isinstance(text, str):
            return text
    parts: list[str] = []
    if isinstance(content, list):
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
    output = message.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            item_content = item.get("content")
            if not isinstance(item_content, list):
                continue
            for part in item_content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    parts.append(part["text"])
    if parts:
        return "\n".join(parts)
    generation = message.get("generation")
    if isinstance(generation, str):
        return generation
    return ""


def _message_id(message: dict) -> str:
    value = message.get("id")
    return str(value) if value is not None else ""


def _matched_on(message: dict) -> str:
    if message.get("content") is not None:
        return "content"
    if message.get("output") is not None:
        return "output"
    return "generation"


def _search_messages(
    chat: dict, text: str, limit: int, context_chars: int, page: int
) -> dict:
    """Search one chat's full message branch.

    One match entry per matching message (first occurrence; repeated
    hits are counted in ``occurrences``), so entries are never
    near-duplicates. The whole branch is always scanned, so ``total``
    is the true number of matching messages regardless of paging.
    """
    messages = _branch_messages(chat)
    needle = text.lower()
    matches: list[dict] = []
    for message in messages:
        full = _extract_text(message)
        if not full:
            continue
        lowered = full.lower()
        idx = lowered.find(needle)
        if idx == -1:
            continue
        start = max(0, idx - context_chars // 2)
        matches.append(
            {
                "message_id": _message_id(message),
                "role": message.get("role"),
                "time": message.get("time"),
                "matched_on": _matched_on(message),
                "occurrences": lowered.count(needle),
                "snippet": full[start : start + context_chars],
            }
        )
    start = (page - 1) * limit
    return {
        "chat_id": chat.get("id"),
        "text": text,
        "page": page,
        "page_size": limit,
        "matches": matches[start : start + limit],
        "total": len(matches),
        "messages_scanned": len(messages),
    }


def _usage_view(chat: dict) -> dict:
    """Map the server-computed ``context_usage`` dict (or its absence)
    onto the tool response, tolerating non-numeric fields."""
    context_usage = chat.get("context_usage")
    if not isinstance(context_usage, dict):
        context_usage = None

    def _num(key: str):
        value = context_usage.get(key) if context_usage else None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return int(value)

    return {
        "context_usage": context_usage,
        "context_usage_note": (
            None
            if context_usage
            else "unavailable: Open-WebUI reported no context usage for "
            "this chat (context compaction may be disabled on the instance)"
        ),
        "window_used_tokens": _num("tokens"),
        "window_limit_tokens": _num("threshold"),
        "window_percent": _num("percent"),
    }


@router.get(
    "/usage",
    summary="Inspect a chat's model context usage",
    operation_id="ContextUsage",
)
async def context_usage(request: Request, chat_id: str | None = None) -> dict:
    """
    Inspect the model context usage of a chat.

    Reports the **server-computed** ``context_usage`` from Open-WebUI
    (``tokens`` used, ``threshold`` = context window, ``percent``,
    ``source``) plus lightweight metadata for the newest messages on
    the current branch. ``context_usage`` is ``null`` when Open-WebUI
    has context compaction disabled for the chat/instance.

    ``chat_id`` may be omitted when Open-WebUI forwards the current chat
    (``ENABLE_FORWARD_USER_INFO_HEADERS=true``) — the
    ``X-OpenWebUI-Chat-Id`` header is used.

    All Open-WebUI calls are made as the validated calling user.
    """
    identity = await _resolve_chat_data_identity(request)
    chat_id = _resolve_chat_id(request, chat_id)
    if not chat_id:
        return {
            "chat_id": None,
            "error": (
                "chat_id is required; pass chat_id or set OWUI's "
                "ENABLE_FORWARD_USER_INFO_HEADERS=true to forward the current chat"
            ),
        }
    _validated_chat_id(chat_id)
    _log_tool("/context/usage", identity)
    try:
        chat = await _owui_request("GET", f"/chats/{chat_id}", request, identity)
    except OWUIError as e:
        return {"chat_id": chat_id, "error": str(e)}
    if not isinstance(chat, dict):
        return {
            "chat_id": chat_id,
            "error": "Open-WebUI returned an unexpected chat response",
        }

    messages = _branch_messages(chat)
    return {
        "chat_id": chat.get("id"),
        "title": chat.get("title"),
        "message_count": len(messages),
        "latest_messages": [
            {
                "message_id": _message_id(m),
                "role": m.get("role"),
                "time": m.get("time"),
                "model": m.get("model"),
            }
            for m in messages[-10:]
        ],
        **_usage_view(chat),
    }


@router.post(
    "/compact",
    summary="Manually compact a chat's context",
    operation_id="ContextCompact",
)
async def context_compact(
    request: Request,
    chat_id: str | None = None,
    dry_run: bool = False,
    model: str | None = None,
) -> dict:
    """
    Manually trigger context compaction for a chat.

    Open-WebUI summarizes the current message branch and stores the
    summary as a non-destructive ``contextSummary`` checkpoint on the
    latest message — the stored messages remain searchable via
    ContextSearch. The server may still do nothing (compaction
    disabled on the instance, or the branch too short): check
    ``compacted`` and ``reason`` in the response rather than assuming
    the call changed anything.

    Note: this is a write to the chat's context state
    (destructive-adjacent) — the chat's future model context may be
    summarized. Set ``dry_run`` to inspect the chat without calling
    the compaction endpoint. ``chat_id`` may be omitted when OWUI
    forwards the current chat (``X-OpenWebUI-Chat-Id``).
    """
    identity = await _resolve_chat_data_identity(request)
    chat_id = _resolve_chat_id(request, chat_id)
    if not chat_id:
        return {
            "chat_id": None,
            "error": (
                "chat_id is required; pass chat_id or set OWUI's "
                "ENABLE_FORWARD_USER_INFO_HEADERS=true to forward the current chat"
            ),
        }
    _validated_chat_id(chat_id)
    _log_tool("/context/compact", identity)

    if dry_run:
        try:
            chat = await _owui_request("GET", f"/chats/{chat_id}", request, identity)
        except OWUIError as e:
            return {"chat_id": chat_id, "error": str(e)}
        if not isinstance(chat, dict):
            return {
                "chat_id": chat_id,
                "error": "Open-WebUI returned an unexpected chat response",
            }
        messages = _branch_messages(chat)
        return {
            "chat_id": chat.get("id"),
            "dry_run": True,
            "would_compact": len(messages) >= 2,
            "message_count": len(messages),
            **_usage_view(chat),
        }

    try:
        result = await _owui_request(
            "POST",
            f"/chats/{chat_id}/compact",
            request,
            identity,
            json={"model": model} if model else {},
        )
    except OWUIError as e:
        return {"chat_id": chat_id, "error": str(e)}
    if not isinstance(result, dict):
        return {
            "chat_id": chat_id,
            "error": "Open-WebUI returned an unexpected compact response",
        }
    return {
        "chat_id": chat_id,
        "ok": result.get("ok", True),
        "compacted": bool(result.get("compacted", False)),
        "reason": result.get("reason"),
        "dropped_messages": result.get("dropped_messages"),
        "kept_messages": result.get("kept_messages"),
        "summary_chars": result.get("summary_chars"),
        "context_usage": (
            result.get("context_usage")
            if isinstance(result.get("context_usage"), dict)
            else None
        ),
    }


@router.get(
    "/search",
    summary="Search a chat's full un-compacted message history",
    operation_id="ContextSearch",
)
async def context_search(
    request: Request,
    text: str = Query(
        min_length=1,
        description="Substring to find in the chat history",
    ),
    chat_id: str | None = None,
    limit: int = Query(
        20,
        ge=1,
        le=100,
        description="Page size in single-chat mode (1-100)",
    ),
    context_chars: int = Query(
        200,
        ge=10,
        le=4000,
        description="Snippet size in characters, single-chat mode (10-4000)",
    ),
    page: int = Query(1, ge=1, description="Page of matches (1-based)"),
) -> dict:
    """
    Recall dropped detail by searching message history.

    With ``chat_id``: scans the chat's full message branch locally and
    returns one match per matching message (``limit`` per page,
    ``total`` = all matching messages, snippets of ``context_chars``).

    Without ``chat_id``: uses Open-WebUI's server-side
    ``GET /chats/search`` — strictly the caller's own chats, paginated
    at 60 chats per page, with a snippet per chat. ``limit`` and
    ``context_chars`` only apply in single-chat mode.

    All Open-WebUI calls are made as the validated calling user — a
    user can only ever see their own chats.
    """
    identity = await _resolve_chat_data_identity(request)
    _log_tool("/context/search", identity)

    if chat_id:
        _validated_chat_id(chat_id)
        try:
            chat = await _owui_request("GET", f"/chats/{chat_id}", request, identity)
        except OWUIError as e:
            return {"chat_id": chat_id, "error": str(e)}
        if not isinstance(chat, dict):
            return {
                "chat_id": chat_id,
                "error": "Open-WebUI returned an unexpected chat response",
            }
        return _search_messages(chat, text, limit, context_chars, page)

    try:
        items = await _owui_request(
            "GET",
            "/chats/search",
            request,
            identity,
            params={"text": text, "page": page},
        )
    except OWUIError as e:
        return {"error": str(e)}
    if not isinstance(items, list):
        return {"error": "Open-WebUI returned an unexpected search response"}
    results = [
        {
            "chat_id": item.get("id"),
            "title": item.get("title"),
            "snippet": item.get("snippet"),
            "updated_at": item.get("updated_at"),
            "archived": bool(item.get("archived", False)),
        }
        for item in items
        if isinstance(item, dict)
    ]
    return {
        "query": text,
        "page": page,
        "page_size": OWUI_SEARCH_PAGE_SIZE,
        "chats_matched": len(results),
        "has_more": len(results) >= OWUI_SEARCH_PAGE_SIZE,
        "results": results,
    }


@router.get(
    "/health",
    summary="Check OWUI connection and identity",
    operation_id="ContextHealth",
)
async def context_health(request: Request) -> dict:
    """
    Verify Open-WebUI is configured and reachable using the acting
    identity (forwarded token, else static OWUI_TOKEN) and report
    which identity resolved.

    Diagnostic-only: unlike the chat-data endpoints, this endpoint may
    fall back to the static ``OWUI_TOKEN`` when no forwarded token is
    present. Because this endpoint itself is unauthenticated, a
    resolved *static* identity is **redacted** (no email/role/user_id
    leakage): present a forwarded user token to see full identity
    details.
    """
    if not settings.is_owui_configured():
        raise ConfigError("Open-WebUI is not configured. Set OWUI_INSTANCE_URL.")
    identity = await resolve_identity(request)
    await _owui_request("GET", "/chats/archived/count", request, identity)
    if identity.source == "forwarded":
        identity_out = {
            "source": identity.source,
            "email": identity.email,
            "role": identity.role,
            "user_id": identity.user_id,
        }
    else:
        identity_out = {
            "source": identity.source,
            "redacted": True,
            "note": (
                "static-token identity is redacted on this unauthenticated "
                "endpoint; present a forwarded user token to see details"
            ),
        }
    return {
        "status": "ok",
        "instance": settings.OWUI_INSTANCE_URL,
        "identity": identity_out,
        "context_endpoints": {
            "usage": True,
            "compact": True,
            "search": True,
            "chat_id_header": CHAT_ID_HEADER,
        },
        "time": datetime.now(UTC).isoformat(),
    }
