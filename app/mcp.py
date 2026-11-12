"""Minimal Streamable HTTP client for Model Context Protocol (MCP) servers.

The inventory tools use this module to drive the ``silverbullet-mcp``
server that Open-WebUI itself uses for SilverBullet access, so both
sides call the same tool set.

Only the pieces the companion needs are implemented — the
``initialize`` handshake and single ``tools/call`` requests — keeping
the dependency surface at plain JSON-RPC 2.0 over ``httpx``.

Wire format (MCP "Streamable HTTP" transport): a single HTTP endpoint
(``SILVERBULLET_MCP_URL``, typically ``http://silverbullet-mcp:4000/mcp``)
that accepts JSON-RPC messages via POST. The server may answer with
plain ``application/json`` *or* a ``text/event-stream`` (SSE) stream
carrying the JSON-RPC message; both are handled transparently. Sessions
are tracked via the ``Mcp-Session-Id`` response header and are
re-established automatically if the server reports the session expired.
"""

from __future__ import annotations

import asyncio
import itertools
import json
from typing import Any

import httpx

from app.errors import MCPError
from app.logging import get_logger

logger = get_logger(__name__)

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "open-webui-companion", "version": "1.0.0"}


def _sse_messages(body: str) -> list[dict]:
    """Parse the JSON-RPC messages out of an SSE stream body."""
    messages: list[dict] = []
    data: list[str] = []

    def flush() -> None:
        for chunk in data:
            if not chunk or chunk == "[DONE]":
                continue
            try:
                msg = json.loads(chunk)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(msg, dict) and ("result" in msg or "error" in msg):
                messages.append(msg)
        data.clear()

    for raw in body.splitlines():
        line = raw.strip()
        if line.startswith("data:"):
            data.append(line[len("data:") :].strip())
        elif not line and data:
            flush()
    flush()
    return messages


class MCPClient:
    """A Streamable HTTP MCP session.

    One instance is shared per process (see ``app.inventory``); all
    requests on the session are serialized.
    """

    def __init__(self, url: str, token: str = "", timeout: float = 30.0) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._http: httpx.AsyncClient | None = None
        self._session_id: str | None = None
        self._initialized = False
        self._lock = asyncio.Lock()
        self._ids = itertools.count(1)

    # -- plumbing -------------------------------------------------------

    async def _http_client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            }
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
            self._http = httpx.AsyncClient(timeout=self.timeout, headers=headers)
        return self._http

    async def aclose(self) -> None:
        """Close the underlying HTTP client."""
        if self._http is not None and not self._http.is_closed:
            await self._http.aclose()
        self._http = None

    def _reset_session(self) -> None:
        self._session_id = None
        self._initialized = False

    async def _post(self, payload: dict[str, Any]) -> httpx.Response:
        http = await self._http_client()
        headers: dict[str, str] = {}
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        try:
            return await http.post(self.url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise MCPError(
                f"Cannot reach silverbullet-mcp at {self.url}: {exc}"
            ) from exc

    @staticmethod
    def _extract_message(resp: httpx.Response, request_id: int | None) -> dict | None:
        """Pull the JSON-RPC response message out of a JSON or SSE body."""
        ctype = resp.headers.get("content-type", "")
        if "text/event-stream" not in ctype:
            try:
                parsed = json.loads(resp.text) if resp.text.strip() else None
            except (json.JSONDecodeError, ValueError):
                parsed = None
            if isinstance(parsed, dict):
                if request_id is not None and parsed.get("id") != request_id:
                    return None
                return parsed
        messages = _sse_messages(resp.text)
        if request_id is not None:
            for msg in messages:
                if msg.get("id") == request_id:
                    return msg
            return None
        return messages[-1] if messages else None

    async def _rpc(self, payload: dict[str, Any], _retry: bool = True) -> dict | None:
        """Send one JSON-RPC message.

        Returns the response message, or ``None`` for notifications /
        empty responses. Raises :class:`MCPError` on transport, protocol
        or JSON-RPC errors.
        """
        resp = await self._post(payload)
        session_header = resp.headers.get("mcp-session-id")
        if session_header:
            self._session_id = session_header

        if resp.status_code in (400, 404, 410) and self._initialized and _retry:
            if "session" in resp.text.lower():
                logger.info("MCP session expired; re-initializing", url=self.url)
                self._reset_session()
                await self._do_initialize()
                return await self._rpc(payload, _retry=False)

        if resp.status_code == 401 or resp.status_code == 403:
            raise MCPError(
                f"silverbullet-mcp rejected authentication (HTTP {resp.status_code}). "
                "Check SILVERBULLET_MCP_TOKEN."
            )
        if resp.status_code in (200, 202, 204):
            if "id" not in payload or not resp.content:
                return None
            msg = self._extract_message(resp, payload.get("id"))
            if msg is None:
                raise MCPError(
                    f"silverbullet-mcp returned no JSON-RPC message (HTTP {resp.status_code})"
                )
            if "error" in msg:
                err = msg["error"]
                detail = err.get("message") if isinstance(err, dict) else str(err)
                raise MCPError(f"silverbullet-mcp JSON-RPC error: {detail}")
            return msg

        raise MCPError(
            f"silverbullet-mcp returned HTTP {resp.status_code}: {resp.text[:300]}",
            status_code=502,
            response_data={"status_code": resp.status_code, "body": resp.text[:300]},
        )

    # -- handshake ------------------------------------------------------

    async def _do_initialize(self) -> None:
        if self._initialized:
            return
        msg = await self._rpc(
            {
                "jsonrpc": "2.0",
                "id": 0,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": CLIENT_INFO,
                },
            }
        )
        server_info = None
        if isinstance(msg, dict):
            server_info = (msg.get("result") or {}).get("serverInfo")
        try:
            await self._rpc({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except MCPError as exc:
            logger.warning("MCP initialized notification failed", error=str(exc))
        self._initialized = True
        logger.info("MCP session established", url=self.url, serverInfo=server_info)

    async def _ensure_initialized(self) -> None:
        async with self._lock:
            await self._do_initialize()

    # -- tools ------------------------------------------------------------

    @staticmethod
    def _tool_text(result: dict) -> str:
        parts = [
            c["text"]
            for c in result.get("content") or []
            if isinstance(c, dict) and isinstance(c.get("text"), str)
        ]
        return " | ".join(parts)

    async def call_tool(self, name: str, arguments: dict | None = None) -> Any:
        """Call a tool and return its ``structuredContent``.

        Falls back to the text content (parsed as JSON when possible)
        for servers that do not advertise an output schema.
        """
        await self._ensure_initialized()
        async with self._lock:
            msg = await self._rpc(
                {
                    "jsonrpc": "2.0",
                    "id": next(self._ids),
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments or {}},
                }
            )
        if not isinstance(msg, dict):
            raise MCPError(f"silverbullet-mcp tool '{name}' produced no response")
        result = msg.get("result")
        if not isinstance(result, dict):
            raise MCPError(f"silverbullet-mcp tool '{name}' produced no result")
        if result.get("isError"):
            raise MCPError(
                f"silverbullet-mcp tool '{name}' failed: {self._tool_text(result)}"
            )
        structured = result.get("structuredContent")
        if structured is not None:
            return structured
        text = self._tool_text(result)
        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return text
