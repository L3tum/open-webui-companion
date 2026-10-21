"""Endpoint tests for app.gitea (Gitea REST API)."""

from __future__ import annotations

import httpx
import pytest
import respx

from app.config import settings

GITEA_BASE = "https://gitea.test"
GITEA_API = f"{GITEA_BASE}/api/v1"


@pytest.fixture
def gitea_env(monkeypatch):
    """Configured Gitea environment pointing at the mock instance."""
    monkeypatch.setattr(settings, "GITEA_INSTANCE_URL", GITEA_BASE)
    monkeypatch.setattr(settings, "GITEA_TOKEN", "test-gitea-token")
    return settings


def _user_route(request):
    """The mock GET /user: assert the token header, return a user."""
    assert request.headers.get("authorization") == "token test-gitea-token"
    return httpx.Response(200, json={"id": 1, "login": "octocat"})


def test_health_ok(client, gitea_env):
    """GiteaHealth calls GET /api/v1/user (not /user/info, which 404s)."""
    with respx.mock(assert_all_called=False) as rx:
        rx.get(f"{GITEA_API}/user").mock(side_effect=_user_route)
        resp = client.get("/gitea/health")
    assert resp.status_code == 200
    assert resp.json() == {
        "status": "ok",
        "username": "octocat",
        "instance": GITEA_BASE,
    }


def test_health_not_configured(client, monkeypatch):
    monkeypatch.setattr(settings, "GITEA_TOKEN", "")
    resp = client.get("/gitea/health")
    assert resp.status_code == 200
    assert resp.json() == {
        "status": "error",
        "message": "Gitea is not configured",
    }


def test_health_reports_auth_error(client, gitea_env):
    with respx.mock(assert_all_called=False) as rx:
        rx.get(f"{GITEA_API}/user").mock(
            return_value=httpx.Response(401, json={"message": "token invalid"})
        )
        resp = client.get("/gitea/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "error"
    assert "401" in body["message"]
