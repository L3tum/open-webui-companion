"""Per-user identity resolution for Open-WebUI tool calls.

Security model (verified against the Open-WebUI v0.12.3 contract):

- The only credential accepted for per-user calls is
  ``Authorization: Bearer <token>``. Plain ``X-OpenWebUI-User-*``
  headers are never trusted for authentication — they are advisory
  metadata.
- A token is **validated against the Open-WebUI backend**:
  ``GET /api/v1/users/user/info`` returns 200 only for a real,
  verified user (or a valid API key); 401/403 means the token is
  rejected. Its response body (``user.info``, a self-editable dict)
  is NOT used for identity.
- **Authoritative attributes** (user id, name, email, role) come from
  ``GET /api/v1/users/user/status`` (the full user object). When that
  endpoint is unavailable (e.g. disabled via ``users.enable_status``
  → 403), the identity stays valid with degraded attributes: empty
  name/email, role ``"unknown"``, and the user id taken from the
  token's own unsigned ``id`` claim.
- Open-WebUI session JWTs carry the user id in an **``id`` claim (not
  ``sub``)**. That claim is read without signature verification and is
  used only for audit logging and the signed-header cross-check,
  never as proof of validity.
- **Fail-closed chat-data access**: with ``OWUI_ALLOW_USER_TOKEN=true``
  (default), the chat-data endpoints (``/context/usage``,
  ``/context/compact``, ``/context/search``) require a *validated
  forwarded* token and never fall back to the static ``OWUI_TOKEN``.
  ``/context/health`` is diagnostic-only and may fall back. With
  ``OWUI_ALLOW_USER_TOKEN=false`` every endpoint uses the static token
  (legacy admin mode).
- **Signed cross-check**: when ``OWUI_FORWARD_JWT_SECRET`` is set, a
  forwarded-token request *must* carry a valid
  ``X-OpenWebUI-User-Jwt`` (HS256, ``iss: "open-webui"``); a missing
  or invalid header is a 401. For session JWTs the header's ``sub``
  (the caller's user id) and/or its ``email`` must match the
  backend-confirmed identity — either positive match accepts, and an
  explicit mismatch on either is a 401. For ``sk-`` API keys only the
  header's authenticity is verified (it describes the calling user,
  not the key owner).
- Validated identities are cached for ``OWUI_TOKEN_CACHE_TTL``
  seconds; rejected tokens get a 30 s negative cache. The cache is
  keyed by the SHA-256 of the token, so tokens never reach logs or
  disk; it is capped at 1024 entries (oldest entry evicted first).
"""

import hashlib
import time
from dataclasses import dataclass

import httpx
import jwt
from fastapi import HTTPException, Request

from app.config import settings
from app.errors import ConfigError, OWUIError
from app.logging import get_logger

logger = get_logger(__name__)

_NEGATIVE_CACHE_TTL = 30  # seconds to remember a rejected token
_REJECTED = object()  # cache sentinel for rejected tokens
_CACHE_MAX_ENTRIES = 1024  # bound memory under many distinct tokens


@dataclass(frozen=True)
class OwuiIdentity:
    """Validated caller identity.

    ``source`` is fixed at construction ("forwarded" | "static") so a
    cached identity can never be re-attributed by a later request (F6).
    """

    token: str
    source: str
    user_id: str | None
    name: str
    email: str
    role: str

    @property
    def auth_header(self) -> dict:
        """Authorization header for forwarding this identity's token."""
        return {"Authorization": f"Bearer {self.token}"}


# token hash -> (timestamp, identity | _REJECTED)
_token_cache: dict[str, tuple[float, object]] = {}


def _cache_key(token: str) -> str:
    return "owui:" + hashlib.sha256(token.encode()).hexdigest()


def _cache_lookup(key: str) -> object | None:
    """Return the cached value, or None on miss/expiry (and evict)."""
    entry = _token_cache.get(key)
    if entry is None:
        return None
    timestamp, value = entry
    ttl = _NEGATIVE_CACHE_TTL if value is _REJECTED else settings.OWUI_TOKEN_CACHE_TTL
    if time.time() - timestamp >= ttl:
        _token_cache.pop(key, None)
        return None
    return value


def _cache_store(key: str, value: object) -> None:
    if len(_token_cache) >= _CACHE_MAX_ENTRIES:
        # Evict the oldest entry to keep the cache bounded.
        oldest_key = min(_token_cache, key=lambda k: _token_cache[k][0])
        _token_cache.pop(oldest_key, None)
    _token_cache[key] = (time.time(), value)


def _user_id_from_token(token: str) -> str | None:
    """Best-effort read of the user id from the session JWT.

    OWUI session JWTs carry the user id in the ``id`` claim (``sub``
    accepted as a fallback for forward compatibility). The decode does
    NOT verify the signature — validity is established by the backend
    round trip, not by local decoding.
    """
    try:
        claims = jwt.decode(
            token,
            options={
                "verify_signature": False,
                "verify_exp": False,
                "verify_iat": False,
                "verify_aud": False,
            },
        )
    except jwt.PyJWTError:
        return None
    user_id = claims.get("id") or claims.get("sub")
    return str(user_id) if user_id else None


async def _owui_get(url: str, headers: dict) -> httpx.Response:
    async with httpx.AsyncClient(timeout=10.0) as client:
        return await client.get(url, headers=headers)


async def _validate_token(
    token: str, source: str, unsigned_id: str | None = None
) -> OwuiIdentity | None:
    """Validate a Bearer token against the Open-WebUI backend.

    Returns an :class:`OwuiIdentity`, or ``None`` if the backend
    rejected the token (401/403). Raises :class:`OWUIError` for
    transport/5xx failures so callers can distinguish "backend
    unavailable" from "token rejected" — and neither is ever cached as
    valid.

    ``unsigned_id`` (the token's unsigned ``id`` claim, read without
    signature verification) may be passed by callers that already
    computed it (e.g. for the pre-validation cross-check) so the token
    is decoded only once per request.
    """
    key = _cache_key(token)
    cached = _cache_lookup(key)
    if cached is not None:
        return None if cached is _REJECTED else cached

    headers = {"Authorization": f"Bearer {token}"}

    # 1) Validation authority: 200 only for a real, verified user (or a
    #    valid API key). The response body is user.info — a self-editable
    #    dict — and is intentionally not used for identity.
    try:
        response = await _owui_get(
            f"{settings.owui_api_url}/users/user/info", headers=headers
        )
    except httpx.RequestError as e:
        raise OWUIError(f"Open-WebUI unavailable during token validation: {e}") from e
    if response.status_code in (401, 403):
        logger.warning(
            "Open-WebUI rejected token",
            source=source,
            status_code=response.status_code,
        )
        _cache_store(key, _REJECTED)
        return None
    if response.status_code != 200:
        raise OWUIError(
            f"Open-WebUI token validation failed: HTTP {response.status_code}",
            status_code=response.status_code,
        )

    # 2) Authoritative attributes from the full user object. /user/status
    #    403s when the instance disables user status; any failure here
    #    degrades attributes but never rejects the (already-validated)
    #    token.
    if unsigned_id is None:
        unsigned_id = _user_id_from_token(token)
    user_id = unsigned_id
    name = email = ""
    role = "unknown"
    try:
        status = await _owui_get(
            f"{settings.owui_api_url}/users/user/status", headers=headers
        )
        if status.status_code == 200:
            data = status.json() or {}
            if data.get("id"):
                user_id = str(data["id"])
            name = str(data.get("name") or "")
            email = str(data.get("email") or "")
            role = str(data.get("role") or "unknown")
        else:
            logger.warning(
                "User status lookup failed",
                source=source,
                status_code=status.status_code,
            )
    except (httpx.RequestError, ValueError) as e:
        logger.warning("User status lookup error", source=source, error=str(e))

    identity = OwuiIdentity(
        token=token,
        source=source,
        user_id=user_id,
        name=name,
        email=email,
        role=role,
    )
    _cache_store(key, identity)
    return identity


def _bearer_token(request: Request) -> str:
    """Extract the Bearer token from the Authorization header ("" if absent)."""
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        return auth[len("Bearer ") :].strip()
    return ""


def _decode_forward_header(request: Request) -> dict | None:
    """Decode + verify the signed ``X-OpenWebUI-User-Jwt`` header.

    Returns the verified claims, or ``None`` when the check is disabled
    (no ``OWUI_FORWARD_JWT_SECRET``). Raises 401 when the secret is set
    but the header is missing or invalid — fail-closed, so a MITM
    cannot strip the header to skip the check.
    """
    if not settings.OWUI_FORWARD_JWT_SECRET:
        return None
    forwarded = request.headers.get("X-OpenWebUI-User-Jwt")
    if not forwarded:
        raise HTTPException(
            401,
            "X-OpenWebUI-User-Jwt header is required when "
            "OWUI_FORWARD_JWT_SECRET is set",
        )
    try:
        return jwt.decode(
            forwarded,
            settings.OWUI_FORWARD_JWT_SECRET,
            algorithms=["HS256"],
            issuer="open-webui",
        )
    except jwt.PyJWTError as e:
        raise HTTPException(401, f"Forwarded identity cross-check failed: {e}") from e


def _cross_check_pre_validation(
    token: str, header_claims: dict | None, unsigned_id: str | None
) -> None:
    """Local (no network) part of the cross-check.

    Compares the verified header's ``sub`` with the *unsigned* ``id``
    claim of the presented JWT, so a header minted for a different user
    is rejected before any backend round-trip. API keys (``sk-``) have
    no user id in the token; the header only proves itself authentic.
    """
    if header_claims is None or token.startswith("sk-"):
        return
    sub = header_claims.get("sub")
    if unsigned_id is not None and sub is not None and str(sub) != str(unsigned_id):
        raise HTTPException(
            401, "Forwarded identity cross-check failed: user id mismatch"
        )


def _cross_check_forwarded_identity(
    token: str, identity: OwuiIdentity, header_claims: dict | None
) -> None:
    """Post-validation part of the cross-check.

    Verifies the header against the *backend-confirmed* identity
    (trusted user id from /users/user/status plus email). Acceptance:
    the header's ``sub`` matches the identity's user id, **or** the
    header's ``email`` matches the identity's email — one positive
    match is enough (the header is signed, so either field ties it to
    this user). An explicit mismatch on either field, or a header that
    matches neither, is a 401. For ``sk-`` API keys the header describes
    the calling user, not the key owner, so only its authenticity
    matters (already verified).
    """
    if header_claims is None or token.startswith("sk-"):
        return
    sub = header_claims.get("sub")
    fwd_email = header_claims.get("email")
    if (
        sub is not None
        and identity.user_id is not None
        and str(sub) != str(identity.user_id)
    ):
        raise HTTPException(
            401, "Forwarded identity cross-check failed: user id mismatch"
        )
    if (
        identity.email
        and isinstance(fwd_email, str)
        and fwd_email
        and identity.email != fwd_email
    ):
        raise HTTPException(
            401, "Forwarded identity cross-check failed: email mismatch"
        )
    id_match = (
        sub is not None
        and identity.user_id is not None
        and str(sub) == str(identity.user_id)
    )
    email_match = (
        bool(identity.email)
        and isinstance(fwd_email, str)
        and (identity.email == fwd_email)
    )
    if not (id_match or email_match):
        raise HTTPException(
            401,
            "Forwarded identity cross-check failed: identity could not be verified",
        )


async def _resolve(request: Request, allow_static_fallback: bool) -> OwuiIdentity:
    """Shared identity resolution.

    ``allow_static_fallback=True``: validated forwarded token →
    validated static ``OWUI_TOKEN`` → :class:`ConfigError` (diagnostic
    / legacy path). ``False``: a validated forwarded token or a 401 —
    the static token never satisfies this (fail-closed chat-data path).

    A *presented* token that the backend rejects is always a 401 — it
    never silently falls back to the static token. With
    ``OWUI_ALLOW_USER_TOKEN=false`` (legacy mode) forwarded tokens are
    ignored entirely.
    """
    if settings.OWUI_ALLOW_USER_TOKEN:
        token = _bearer_token(request)
        if token:
            header_claims = _decode_forward_header(request)
            unsigned_id = _user_id_from_token(token)
            _cross_check_pre_validation(token, header_claims, unsigned_id)
            identity = await _validate_token(
                token, source="forwarded", unsigned_id=unsigned_id
            )
            if identity is None:
                raise HTTPException(
                    401, "Forwarded Open-WebUI token was rejected by the backend"
                )
            _cross_check_forwarded_identity(token, identity, header_claims)
            return identity
    if allow_static_fallback:
        if settings.OWUI_TOKEN:
            identity = await _validate_token(settings.OWUI_TOKEN, source="static")
            if identity is None:
                raise ConfigError("OWUI_TOKEN is set but was rejected by Open-WebUI")
            return identity
        raise ConfigError("No forwarded user token and OWUI_TOKEN is not set")
    raise HTTPException(
        401,
        "A forwarded Open-WebUI user token "
        "(Authorization: Bearer) is required for this endpoint",
    )


async def resolve_identity(request: Request) -> OwuiIdentity:
    """Resolve the acting identity for diagnostic/legacy use.

    Forwarded Bearer token (validated, cross-checked) → static
    ``OWUI_TOKEN`` (validated) → :class:`ConfigError`.
    """
    return await _resolve(request, allow_static_fallback=True)


async def resolve_forwarded_identity(request: Request) -> OwuiIdentity:
    """Strict identity for chat-data endpoints (fail-closed).

    Requires a validated forwarded token and the signed-header
    cross-check when enabled; the static ``OWUI_TOKEN`` never satisfies
    this. Raises 401 when the token is absent, rejected, or fails the
    cross-check.
    """
    return await _resolve(request, allow_static_fallback=False)
