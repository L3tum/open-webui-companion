"""Unit tests for app.identity: validation, fallback order, cache, cross-check.

The mocked OWUI follows the real v0.12.3 contract: /users/user/info is
the validation authority (returns user.info, typically {}), and
/users/user/status returns the full user object. Session JWTs carry the
user id in the ``id`` claim (see tests/conftest.py).
"""

from __future__ import annotations

import asyncio
import dataclasses

import httpx
import pytest
import respx
from fastapi import HTTPException

from app import identity
from app.config import settings
from app.errors import ConfigError, OWUIError
from tests.conftest import (
    FWD_SECRET,
    OWUI_API,
    make_forward_jwt,
    make_request,
    make_user_jwt,
)

INFO_URL = f"{OWUI_API}/users/user/info"
STATUS_URL = f"{OWUI_API}/users/user/status"


def _record(user_id, name, email, role):
    return {"id": user_id, "name": name, "email": email, "role": role, "info": {}}


ALICE = _record("u-alice", "Alice", "alice@example.com", "pending")
ADMIN = _record("u-admin", "Admin", "admin@example.com", "admin")


def _bearer(request):
    auth = request.headers.get("authorization", "")
    return auth[len("Bearer ") :] if auth.startswith("Bearer ") else ""


def _mock_info(rx, token_records):
    """Mock /users/user/info: 200 (user.info body) for known tokens, 401 otherwise."""

    def side_effect(request):
        if _bearer(request) not in token_records:
            return httpx.Response(401, json={"detail": "Invalid token"})
        return httpx.Response(200, json=token_records[_bearer(request)]["info"])

    return rx.get(INFO_URL).mock(side_effect=side_effect)


def _mock_status(rx, token_records, status_code=200):
    """Mock /users/user/status: 200 (full user object) for known tokens."""

    def side_effect(request):
        if _bearer(request) not in token_records:
            return httpx.Response(401, json={"detail": "Invalid token"})
        if status_code == 403:
            return httpx.Response(403, json={"detail": "User status is disabled"})
        return httpx.Response(200, json=token_records[_bearer(request)])

    return rx.get(STATUS_URL).mock(side_effect=side_effect)


def test_user_id_from_token_id_claim():
    assert identity._user_id_from_token(make_user_jwt("u-42")) == "u-42"


def test_user_id_from_token_sub_fallback():
    assert identity._user_id_from_token(make_user_jwt("u-42", sub="u-42")) == "u-42"


def test_user_id_from_token_garbage():
    assert identity._user_id_from_token("not-a-jwt") is None


def test_validate_token_attributes_from_status():
    """Valid token -> identity whose attributes come from /users/user/status."""
    token = make_user_jwt("u-alice")
    records = {token: ALICE}
    with respx.mock(assert_all_called=False) as rx:
        _mock_info(rx, records)
        _mock_status(rx, records)
        ident = asyncio.run(identity._validate_token(token, source="forwarded"))

    assert ident is not None
    assert ident.user_id == "u-alice"
    assert ident.name == "Alice"
    assert ident.email == "alice@example.com"
    assert ident.role == "pending"
    assert ident.source == "forwarded"


def test_validate_token_status_disabled_degrades():
    """/user/status 403 (users.enable_status off) -> identity still valid,
    attributes degraded, user id from the JWT's own id claim."""
    token = make_user_jwt("u-alice")
    records = {token: ALICE}
    with respx.mock(assert_all_called=False) as rx:
        _mock_info(rx, records)
        _mock_status(rx, records, status_code=403)
        ident = asyncio.run(identity._validate_token(token, source="forwarded"))

    assert ident is not None
    assert ident.user_id == "u-alice"  # from the unsigned id claim
    assert ident.name == ""
    assert ident.email == ""
    assert ident.role == "unknown"


def test_validate_token_rejected():
    """401 from /users/user/info -> None (token rejected)."""
    token = make_user_jwt("u-unknown")
    with respx.mock(assert_all_called=False) as rx:
        _mock_info(rx, {})
        assert asyncio.run(identity._validate_token(token, source="forwarded")) is None


def test_validate_token_owui_unavailable():
    """Transport failure -> OWUIError (distinct from a rejection)."""
    token = make_user_jwt("u-alice")
    with respx.mock(assert_all_called=False) as rx:
        rx.get(INFO_URL).mock(side_effect=httpx.ConnectError("unreachable"))
        with pytest.raises(OWUIError):
            asyncio.run(identity._validate_token(token, source="forwarded"))


def test_rejected_token_is_negative_cached():
    """A rejected token is remembered; the backend is not hit again."""
    token = make_user_jwt("u-unknown")
    with respx.mock(assert_all_called=False) as rx:
        route = _mock_info(rx, {})
        _mock_status(rx, {})
        assert asyncio.run(identity._validate_token(token, source="forwarded")) is None
        assert asyncio.run(identity._validate_token(token, source="forwarded")) is None

    assert route.call_count == 1


def test_validation_is_cached():
    """Two validations of the same token hit /users/user/info exactly once."""
    token = make_user_jwt("u-alice")
    records = {token: ALICE}
    with respx.mock(assert_all_called=False) as rx:
        info_route = _mock_info(rx, records)
        status_route = _mock_status(rx, records)
        ident1 = asyncio.run(identity._validate_token(token, source="forwarded"))
        ident2 = asyncio.run(identity._validate_token(token, source="forwarded"))

    assert ident1 is not None and ident2 is not None
    assert info_route.call_count == 1
    assert status_route.call_count == 1


def test_identity_source_is_immutable():
    """A cached identity can never be re-attributed by a later request."""
    ident = identity.OwuiIdentity(
        token="t", source="static", user_id=None, name="", email="", role="unknown"
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        ident.source = "forwarded"


def test_resolve_identity_forwarded():
    """Valid forwarded token -> identity with source 'forwarded'."""
    token = make_user_jwt("u-alice")
    records = {token: ALICE}
    with respx.mock(assert_all_called=False) as rx:
        _mock_info(rx, records)
        _mock_status(rx, records)
        ident = asyncio.run(
            identity.resolve_identity(
                make_request({"Authorization": f"Bearer {token}"})
            )
        )

    assert ident.source == "forwarded"
    assert ident.token == token
    assert ident.email == "alice@example.com"


def test_resolve_rejected_forwarded_401(monkeypatch):
    """Rejected forwarded token -> 401, even when a valid static token exists
    (no silent fallback to the static identity)."""
    forwarded = make_user_jwt("u-unknown")
    static = make_user_jwt("u-admin")
    monkeypatch.setattr(settings, "OWUI_TOKEN", static)
    records = {static: ADMIN}
    with respx.mock(assert_all_called=False) as rx:
        _mock_info(rx, records)
        _mock_status(rx, records)
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                identity.resolve_identity(
                    make_request({"Authorization": f"Bearer {forwarded}"})
                )
            )
    assert exc.value.status_code == 401


def test_no_auth_header_uses_static(monkeypatch):
    """No Authorization header -> static OWUI_TOKEN identity."""
    static = make_user_jwt("u-admin")
    monkeypatch.setattr(settings, "OWUI_TOKEN", static)
    records = {static: ADMIN}
    with respx.mock(assert_all_called=False) as rx:
        _mock_info(rx, records)
        _mock_status(rx, records)
        ident = asyncio.run(identity.resolve_identity(make_request({})))

    assert ident.source == "static"
    assert ident.token == static


def test_invalid_static_raises_config_error(monkeypatch):
    """A static OWUI_TOKEN the backend rejects is a configuration error."""
    monkeypatch.setattr(settings, "OWUI_TOKEN", make_user_jwt("u-unknown"))
    with respx.mock(assert_all_called=False) as rx:
        _mock_info(rx, {})
        _mock_status(rx, {})
        with pytest.raises(ConfigError):
            asyncio.run(identity.resolve_identity(make_request({})))


def test_no_token_at_all_raises_config_error():
    with pytest.raises(ConfigError):
        asyncio.run(identity.resolve_identity(make_request({})))


def test_forwarded_resolver_requires_bearer(monkeypatch):
    """Chat-data resolver: no Bearer token -> 401, static token never sent."""
    static = make_user_jwt("u-admin")
    monkeypatch.setattr(settings, "OWUI_TOKEN", static)
    records = {static: ADMIN}
    with respx.mock(assert_all_called=False) as rx:
        _mock_info(rx, records)
        _mock_status(rx, records)
        with pytest.raises(HTTPException) as exc:
            asyncio.run(identity.resolve_forwarded_identity(make_request({})))

    assert exc.value.status_code == 401
    assert rx.calls.call_count == 0  # the static token was never sent


def test_forwarded_resolver_rejects_invalid(monkeypatch):
    static = make_user_jwt("u-admin")
    monkeypatch.setattr(settings, "OWUI_TOKEN", static)
    records = {static: ADMIN}
    with respx.mock(assert_all_called=False) as rx:
        _mock_info(rx, records)
        _mock_status(rx, records)
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                identity.resolve_forwarded_identity(
                    make_request({"Authorization": "Bearer u-unknown-jwt"})
                )
            )
    assert exc.value.status_code == 401


def test_forwarded_resolver_ok():
    token = make_user_jwt("u-alice")
    records = {token: ALICE}
    with respx.mock(assert_all_called=False) as rx:
        _mock_info(rx, records)
        _mock_status(rx, records)
        ident = asyncio.run(
            identity.resolve_forwarded_identity(
                make_request({"Authorization": f"Bearer {token}"})
            )
        )

    assert ident.source == "forwarded"
    assert ident.email == "alice@example.com"


def test_allow_user_token_false_ignores_forwarded(monkeypatch):
    """OWUI_ALLOW_USER_TOKEN=false (legacy mode) -> forwarded tokens ignored."""
    forwarded = make_user_jwt("u-alice")
    static = make_user_jwt("u-admin")
    monkeypatch.setattr(settings, "OWUI_TOKEN", static)
    monkeypatch.setattr(settings, "OWUI_ALLOW_USER_TOKEN", False)
    records = {static: ADMIN}
    with respx.mock(assert_all_called=False) as rx:
        info_route = _mock_info(rx, records)
        _mock_status(rx, records)
        ident = asyncio.run(
            identity.resolve_identity(
                make_request({"Authorization": f"Bearer {forwarded}"})
            )
        )

    assert ident.source == "static"
    assert info_route.call_count == 1
    assert info_route.calls.last.request.headers["authorization"] == f"Bearer {static}"


def test_cross_check_matching_header(monkeypatch):
    """Configured secret + matching signed header (sub == id claim) -> accepted."""
    token = make_user_jwt("u-alice")
    monkeypatch.setattr(settings, "OWUI_FORWARD_JWT_SECRET", FWD_SECRET)
    records = {token: ALICE}
    with respx.mock(assert_all_called=False) as rx:
        _mock_info(rx, records)
        _mock_status(rx, records)
        ident = asyncio.run(
            identity.resolve_identity(
                make_request(
                    {
                        "Authorization": f"Bearer {token}",
                        "X-OpenWebUI-User-Jwt": make_forward_jwt(
                            sub="u-alice", email="alice@example.com"
                        ),
                    }
                )
            )
        )

    assert ident.source == "forwarded"


def test_cross_check_sub_mismatch_401(monkeypatch):
    """Signed header with a different sub -> 401 (never falls back to static)."""
    token = make_user_jwt("u-alice")
    static = make_user_jwt("u-admin")
    monkeypatch.setattr(settings, "OWUI_TOKEN", static)
    monkeypatch.setattr(settings, "OWUI_FORWARD_JWT_SECRET", FWD_SECRET)
    records = {token: ALICE, static: ADMIN}
    with respx.mock(assert_all_called=False) as rx:
        _mock_info(rx, records)
        _mock_status(rx, records)
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                identity.resolve_identity(
                    make_request(
                        {
                            "Authorization": f"Bearer {token}",
                            "X-OpenWebUI-User-Jwt": make_forward_jwt(
                                sub="someone-else", email="alice@example.com"
                            ),
                        }
                    )
                )
            )
    assert exc.value.status_code == 401


def test_cross_check_email_mismatch_401(monkeypatch):
    token = make_user_jwt("u-alice")
    monkeypatch.setattr(settings, "OWUI_FORWARD_JWT_SECRET", FWD_SECRET)
    records = {token: ALICE}
    with respx.mock(assert_all_called=False) as rx:
        _mock_info(rx, records)
        _mock_status(rx, records)
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                identity.resolve_identity(
                    make_request(
                        {
                            "Authorization": f"Bearer {token}",
                            "X-OpenWebUI-User-Jwt": make_forward_jwt(
                                sub="u-alice", email="other@example.com"
                            ),
                        }
                    )
                )
            )
    assert exc.value.status_code == 401


def test_cross_check_bad_signature_401(monkeypatch):
    token = make_user_jwt("u-alice")
    monkeypatch.setattr(settings, "OWUI_FORWARD_JWT_SECRET", FWD_SECRET)
    records = {token: ALICE}
    with respx.mock(assert_all_called=False) as rx:
        _mock_info(rx, records)
        _mock_status(rx, records)
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                identity.resolve_identity(
                    make_request(
                        {
                            "Authorization": f"Bearer {token}",
                            "X-OpenWebUI-User-Jwt": make_forward_jwt(
                                sub="u-alice",
                                email="alice@example.com",
                                secret="wrong-secret",
                            ),
                        }
                    )
                )
            )
    assert exc.value.status_code == 401


def test_cross_check_wrong_issuer_401(monkeypatch):
    token = make_user_jwt("u-alice")
    monkeypatch.setattr(settings, "OWUI_FORWARD_JWT_SECRET", FWD_SECRET)
    records = {token: ALICE}
    with respx.mock(assert_all_called=False) as rx:
        _mock_info(rx, records)
        _mock_status(rx, records)
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                identity.resolve_identity(
                    make_request(
                        {
                            "Authorization": f"Bearer {token}",
                            "X-OpenWebUI-User-Jwt": make_forward_jwt(
                                sub="u-alice",
                                email="alice@example.com",
                                issuer="evil",
                            ),
                        }
                    )
                )
            )
    assert exc.value.status_code == 401


def test_cross_check_expired_401(monkeypatch):
    token = make_user_jwt("u-alice")
    monkeypatch.setattr(settings, "OWUI_FORWARD_JWT_SECRET", FWD_SECRET)
    records = {token: ALICE}
    with respx.mock(assert_all_called=False) as rx:
        _mock_info(rx, records)
        _mock_status(rx, records)
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                identity.resolve_identity(
                    make_request(
                        {
                            "Authorization": f"Bearer {token}",
                            "X-OpenWebUI-User-Jwt": make_forward_jwt(
                                sub="u-alice",
                                email="alice@example.com",
                                exp_offset=-300,
                            ),
                        }
                    )
                )
            )
    assert exc.value.status_code == 401


def test_cross_check_missing_header_401(monkeypatch):
    """Secret configured but no signed header -> 401 (fail-closed; a MITM
    can no longer strip the header to skip the check)."""
    token = make_user_jwt("u-alice")
    monkeypatch.setattr(settings, "OWUI_FORWARD_JWT_SECRET", FWD_SECRET)
    records = {token: ALICE}
    with respx.mock(assert_all_called=False) as rx:
        _mock_info(rx, records)
        _mock_status(rx, records)
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                identity.resolve_identity(
                    make_request({"Authorization": f"Bearer {token}"})
                )
            )
    assert exc.value.status_code == 401


def test_cross_check_off_by_default(monkeypatch):
    """No secret configured -> no header required."""
    token = make_user_jwt("u-alice")
    monkeypatch.setattr(settings, "OWUI_FORWARD_JWT_SECRET", "")
    records = {token: ALICE}
    with respx.mock(assert_all_called=False) as rx:
        _mock_info(rx, records)
        _mock_status(rx, records)
        ident = asyncio.run(
            identity.resolve_identity(
                make_request({"Authorization": f"Bearer {token}"})
            )
        )

    assert ident.source == "forwarded"


def test_cross_check_api_key_signature_only(monkeypatch):
    """sk- API key: the header describes the calling user, not the key
    owner — only the header's authenticity is verified."""
    monkeypatch.setattr(settings, "OWUI_FORWARD_JWT_SECRET", FWD_SECRET)
    records = {"sk-admin-key": ADMIN}
    with respx.mock(assert_all_called=False) as rx:
        _mock_info(rx, records)
        _mock_status(rx, records)
        ident = asyncio.run(
            identity.resolve_identity(
                make_request(
                    {
                        "Authorization": "Bearer sk-admin-key",
                        "X-OpenWebUI-User-Jwt": make_forward_jwt(
                            sub="u-alice", email="alice@example.com"
                        ),
                    }
                )
            )
        )

    assert ident is not None
    assert ident.source == "forwarded"
    assert ident.role == "admin"  # key owner, from /user/status


def test_cross_check_api_key_bad_signature_401(monkeypatch):
    monkeypatch.setattr(settings, "OWUI_FORWARD_JWT_SECRET", FWD_SECRET)
    records = {"sk-admin-key": ADMIN}
    with respx.mock(assert_all_called=False) as rx:
        _mock_info(rx, records)
        _mock_status(rx, records)
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                identity.resolve_identity(
                    make_request(
                        {
                            "Authorization": "Bearer sk-admin-key",
                            "X-OpenWebUI-User-Jwt": make_forward_jwt(
                                sub="u-alice",
                                email="alice@example.com",
                                secret="wrong-secret",
                            ),
                        }
                    )
                )
            )
    assert exc.value.status_code == 401
