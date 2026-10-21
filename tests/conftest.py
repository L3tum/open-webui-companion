"""Shared pytest fixtures for companion-server tests.

The mocked Open-WebUI endpoints follow the real v0.12.3 contract:

- ``GET /users/user/info`` -> 200 with ``user.info`` (a self-editable
  dict, typically ``{}``) for a valid user, 401 otherwise. It is the
  token-validation authority; the body carries no identity.
- ``GET /users/user/status`` -> 200 with the full user object
  (``id``, ``name``, ``email``, ``role``, ...) for a valid user, 401
  otherwise. Tests may re-mock the route to simulate the endpoint
  being disabled (``users.enable_status`` off -> 403).
- Open-WebUI session JWTs carry the user id in an **``id`` claim**
  (not ``sub``).
- The signed ``X-OpenWebUI-User-Jwt`` header carries ``sub`` (= user
  id), ``email``, ``name``, ``role``, ``iss: "open-webui"``.
"""

import time

import httpx
import jwt
import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.config import settings
from app.identity import _token_cache
from app.main import app

JWT_SECRET = "test-owui-secret"
FWD_SECRET = "test-forward-secret"
OWUI_BASE = "http://owui.test"
OWUI_API = f"{OWUI_BASE}/api/v1"

# API key -> owning user id (OWUI accepts sk- keys via get_current_user)
API_KEY_OWNERS = {"sk-admin-key": "u-admin"}


def _user_record(user_id, name, email, role, info=None):
    return {
        "id": user_id,
        "name": name,
        "email": email,
        "role": role,
        "info": info or {},
    }


@pytest.fixture
def users():
    """The mocked OWUI user directory, keyed by user id."""
    return {
        "u-alice": _user_record("u-alice", "Alice", "alice@example.com", "pending"),
        "u-bob": _user_record("u-bob", "Bob", "bob@example.com", "pending"),
        "u-admin": _user_record("u-admin", "Admin", "admin@example.com", "admin"),
    }


def _bearer(request):
    auth = request.headers.get("authorization", "")
    return auth[len("Bearer ") :] if auth.startswith("Bearer ") else ""


def _token_user_id(token):
    """Resolve the user id a token stands for in the mock backend."""
    if not token:
        return None
    if token in API_KEY_OWNERS:
        return API_KEY_OWNERS[token]
    try:
        claims = jwt.decode(
            token, options={"verify_signature": False, "verify_exp": False}
        )
        return claims.get("id") or claims.get("sub")
    except (jwt.PyJWTError, TypeError, ValueError):
        return None


def make_user_jwt(user_id="u-alice", *, sub=None, exp_offset=3600, secret=JWT_SECRET):
    """Mint an OWUI-style session JWT (user id in the ``id`` claim)."""
    now = int(time.time())
    claims = {"id": user_id, "typ": "session", "iat": now, "exp": now + exp_offset}
    if sub is not None:
        claims["sub"] = sub
    return jwt.encode(claims, secret, algorithm="HS256")


def make_forward_jwt(
    sub,
    email="",
    name="",
    role="user",
    *,
    secret=FWD_SECRET,
    exp_offset=300,
    issuer="open-webui",
):
    """Mint the signed X-OpenWebUI-User-Jwt exactly as OWUI mints it.

    ``sub=None`` omits the claim (tests the email-only match path).
    """
    now = int(time.time())
    claims = {
        "email": email,
        "name": name,
        "role": role,
        "iss": issuer,
        "iat": now,
        "exp": now + exp_offset,
    }
    if sub is not None:
        claims["sub"] = sub
    return jwt.encode(claims, secret, algorithm="HS256")


def make_message(
    message_id,
    role,
    content=None,
    *,
    output_text=None,
    time=1716000000000,
    model=None,
    parent_id=None,
):
    """Build a v0.12.3-shaped message dict.

    User messages carry their text in ``content`` (a string); assistant
    messages carry it in ``output`` parts of type ``message`` — the
    shapes OWUI actually stores.
    """
    message = {"id": message_id, "role": role, "time": time}
    if content is not None:
        message["content"] = content
    if output_text is not None:
        message["output"] = [
            {"type": "message", "content": [{"type": "text", "text": output_text}]}
        ]
    if model is not None:
        message["model"] = model
    if parent_id is not None:
        message["parentId"] = parent_id
    return message


def make_chat(
    chat_id,
    title,
    messages,
    *,
    user_id="u-alice",
    context_usage=None,
    archived=False,
    pinned=False,
):
    """A ChatResponse exactly as GET /chats/{id} returns it in v0.12.3:

    ``chat.history.messages`` is a map of message id -> message and the
    current branch is the ``parentId`` chain ending at
    ``current_message_id`` / ``history.currentId``. ``context_usage`` is
    the server-computed usage dict (absent when context compaction is
    disabled on the instance).
    """
    current_id = messages[-1]["id"] if messages else None
    return {
        "id": chat_id,
        "user_id": user_id,
        "title": title,
        "chat": {
            "history": {
                "messages": {m["id"]: m for m in messages},
                "currentId": current_id,
            }
        },
        "updated_at": 1716000000000,
        "created_at": 1716000000000,
        "share_id": None,
        "archived": archived,
        "pinned": pinned,
        "meta": {},
        "variables": {},
        "folder_id": None,
        "tasks": None,
        "summary": None,
        "current_message_id": current_id,
        "context_usage": context_usage,
    }


def make_context_usage(tokens=1200, threshold=100000, source="estimated"):
    """The server-computed context_usage dict (OWUI context_compaction.py)."""
    return {
        "tokens": tokens,
        "estimated_tokens": tokens,
        "threshold": threshold,
        "percent": round(tokens / threshold * 100) if threshold else 0,
        "source": source,
    }


def make_request(headers=None):
    """Build a bare Starlette Request (headers only) for resolver tests."""
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [
            (k.lower().encode(), v.encode()) for k, v in (headers or {}).items()
        ],
    }
    return Request(scope)


def _rejected():
    return httpx.Response(401, json={"detail": "Invalid token"})


def info_side_effect(users):
    """Factory for the /users/user/info side effect (200 user.info / 401)."""

    def side_effect(request):
        uid = _token_user_id(_bearer(request))
        if uid not in users:
            return _rejected()
        return httpx.Response(200, json=users[uid]["info"])

    return side_effect


def status_side_effect(users, status_code=200):
    """Factory for the /users/user/status side effect (200 full user / 401)."""

    def side_effect(request):
        uid = _token_user_id(_bearer(request))
        if uid not in users:
            return _rejected()
        if status_code == 403:
            return httpx.Response(403, json={"detail": "User status is disabled"})
        return httpx.Response(200, json=users[uid])

    return side_effect


def mock_owui_identity(rx, users, status_code=200):
    """Register the two identity endpoints on a respx mock router."""
    rx.get(f"{OWUI_API}/users/user/info").mock(side_effect=info_side_effect(users))
    rx.get(f"{OWUI_API}/users/user/status").mock(
        side_effect=status_side_effect(users, status_code=status_code)
    )


@pytest.fixture(autouse=True)
def baseline(monkeypatch):
    """Isolated, minimal settings for every test."""
    monkeypatch.setenv("OWUI_INSTANCE_URL", OWUI_BASE)
    monkeypatch.setenv("OWUI_ALLOW_USER_TOKEN", "true")
    monkeypatch.setenv("OWUI_FORWARD_JWT_SECRET", "")
    monkeypatch.setenv("EVIDENCE_DIR", "evidence")
    settings.OWUI_INSTANCE_URL = OWUI_BASE
    settings.OWUI_TOKEN = ""
    settings.OWUI_ALLOW_USER_TOKEN = True
    settings.OWUI_FORWARD_JWT_SECRET = ""
    settings.OWUI_TOKEN_CACHE_TTL = 300
    return settings


@pytest.fixture(autouse=True)
def clear_identity_cache():
    """The identity cache is process-global; never leak entries between tests."""
    _token_cache.clear()
    yield
    _token_cache.clear()


@pytest.fixture
def client(baseline):
    # Deliberately NOT used as a context manager: that would run the app
    # lifespan per test. Without it, ``app.state.owui_client`` is absent
    # and OWUI calls use per-call clients (the path under test).
    return TestClient(app)
