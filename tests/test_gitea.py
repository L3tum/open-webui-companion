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
    assert "404" not in body["message"] or "401" in body["message"]


# ─── PR diff (current Gitea/Forgejo: /pulls/{n}.diff, text response) ───────


DIFF_TEXT = "diff --git a/x.go b/x.go\n+hello"


def test_pr_diff_uses_dot_route_and_returns_text(client, gitea_env):
    """GiteaGetPRDiff must call /pulls/{n}.diff (the /pulls/{n}/diff
    route was removed from Gitea/Forgejo) and return the raw text.
    """
    with respx.mock(assert_all_called=False) as rx:
        legacy = rx.get(f"{GITEA_API}/repos/o/r/pulls/5/diff").mock(
            return_value=httpx.Response(404, json={"message": "not found"})
        )
        rx.get(f"{GITEA_API}/repos/o/r/pulls/5.diff").mock(
            return_value=httpx.Response(
                200, content=DIFF_TEXT.encode(), headers={"Content-Type": "text/plain"}
            )
        )
        resp = client.get("/gitea/pulls/o/r/5/diff")
        assert not legacy.calls, "legacy /pulls/{n}/diff route must not be used"
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert resp.text == DIFF_TEXT


# ─── PR review comments (current Gitea/Forgejo: grouped per review) ───────


def test_pr_comments_fetched_via_reviews(client, gitea_env):
    """GiteaListPRComments must walk /pulls/{n}/reviews and then each
    review's /comments (the flat /pulls/{n}/comments route was removed).
    """
    with respx.mock(assert_all_called=False) as rx:
        rx.get(f"{GITEA_API}/repos/o/r/pulls/5/reviews").mock(
            return_value=httpx.Response(
                200, json=[{"id": 1}, {"id": 2}]
            )
        )
        rx.get(f"{GITEA_API}/repos/o/r/pulls/5/reviews/1/comments").mock(
            return_value=httpx.Response(
                200, json=[{"id": 10, "body": "nit", "pull_request_review_id": 1}]
            )
        )
        rx.get(f"{GITEA_API}/repos/o/r/pulls/5/reviews/2/comments").mock(
            return_value=httpx.Response(
                200, json=[{"id": 11, "body": "must fix", "pull_request_review_id": 2}]
            )
        )
        legacy = rx.get(f"{GITEA_API}/repos/o/r/pulls/5/comments").mock(
            return_value=httpx.Response(404, json={"message": "not found"})
        )
        resp = client.get("/gitea/pulls/o/r/5/comments")
        assert not legacy.calls, "legacy flat comments route must not be used"
    assert resp.status_code == 200
    comments = resp.json()
    assert [c["id"] for c in comments] == [10, 11]
    assert all("pull_request_review_id" in c for c in comments)


def test_pr_comments_falls_back_to_flat_endpoint(client, gitea_env):
    """On very old Gitea (<= 1.23) /reviews 404s; use the flat route."""
    with respx.mock(assert_all_called=False) as rx:
        rx.get(f"{GITEA_API}/repos/o/r/pulls/5/reviews").mock(
            return_value=httpx.Response(404, json={"message": "not found"})
        )
        rx.get(f"{GITEA_API}/repos/o/r/pulls/5/comments").mock(
            return_value=httpx.Response(200, json=[{"id": 1, "body": "old"}])
        )
        resp = client.get("/gitea/pulls/o/r/5/comments")
    assert resp.status_code == 200
    assert resp.json() == [{"id": 1, "body": "old"}]


# ─── PR review submission (event field, uppercase values) ─────────────────


def test_pr_review_sends_event_and_legacy_type(client, gitea_env):
    """GiteaSubmitPRReview must send `event` (current Gitea/Forgejo) and
    legacy `type` (Gitea <= 1.23) with the right value conventions.
    """
    captured: dict = {}

    def _post(request):
        captured["body"] = request.content.decode()
        return httpx.Response(201, json={"id": 1, "state": "APPROVED"})

    import json as _json

    with respx.mock(assert_all_called=False) as rx:
        rx.post(f"{GITEA_API}/repos/o/r/pulls/5/reviews").mock(side_effect=_post)
        resp = client.post(
            "/gitea/pulls/o/r/5/reviews",
            json={"body": "lgtm", "type": "approved"},
        )
    assert resp.status_code == 200
    payload = _json.loads(captured["body"])
    assert payload["event"] == "APPROVED"
    assert payload["type"] == "approve"
    assert payload["body"] == "lgtm"


@pytest.mark.parametrize(
    ("review_type", "event", "legacy"),
    [
        ("approved", "APPROVED", "approve"),
        ("changes_requested", "REQUEST_CHANGES", "request_changes"),
        ("comment", "COMMENT", "comment"),
    ],
)
def test_pr_review_type_mapping(client, gitea_env, review_type, event, legacy):
    import json as _json

    captured: dict = {}

    def _post(request):
        captured["body"] = request.content.decode()
        return httpx.Response(201, json={"id": 1})

    with respx.mock(assert_all_called=False) as rx:
        rx.post(f"{GITEA_API}/repos/o/r/pulls/5/reviews").mock(side_effect=_post)
        payload = {
            "body": "some body",
            "type": review_type,
        }
        resp = client.post("/gitea/pulls/o/r/5/reviews", json=payload)
    assert resp.status_code == 200
    sent = _json.loads(captured["body"])
    assert sent["event"] == event
    assert sent["type"] == legacy


def test_pr_review_request_changes_requires_body(client, gitea_env):
    """Current Gitea rejects REQUEST_CHANGES without a body — the tool
    should fail fast with a 400 instead of letting the server reject it.
    """
    with respx.mock(assert_all_called=False) as rx:
        rx.post(f"{GITEA_API}/repos/o/r/pulls/5/reviews").mock(
            return_value=httpx.Response(201, json={"id": 1})
        )
        resp = client.post(
            "/gitea/pulls/o/r/5/reviews",
            json={"body": "", "type": "changes_requested"},
        )
    assert resp.status_code == 400
    assert "body" in resp.json()["detail"].lower()


# ─── Pipeline: branch/ref filter + run field mapping ──────────────────────


def test_pr_pipeline_sends_branch_and_ref(client, gitea_env):
    """Gitea filters runs by `branch`, Forgejo by `ref` — both must be
    sent so the head-branch filter works on either flavor."""
    seen: dict = {}

    def _runs(request):
        seen["params"] = dict(request.url.params)
        # Forgejo-shaped run: title/prettyref/commit_sha/started/updated
        return httpx.Response(
            200,
            json={
                "total_count": 1,
                "workflow_runs": [
                    {
                        "id": 42,
                        "index_in_repo": 7,
                        "title": "CI build",
                        "status": "success",
                        "event": "pull_request",
                        "prettyref": "feature/x",
                        "commit_sha": "abc123",
                        "html_url": f"{GITEA_BASE}/o/r/actions/runs/42",
                        "started": "2026-07-16T10:00:00Z",
                        "updated": "2026-07-16T10:05:00Z",
                    }
                ],
            },
        )

    with respx.mock(assert_all_called=False) as rx:
        rx.get(f"{GITEA_API}/repos/o/r/pulls/5").mock(
            return_value=httpx.Response(
                200, json={"number": 5, "head": {"ref": "feature/x"}}
            )
        )
        rx.get(f"{GITEA_API}/repos/o/r/actions/runs").mock(side_effect=_runs)
        resp = client.get("/gitea/pulls/o/r/5/pipeline")
    assert resp.status_code == 200
    assert seen["params"]["branch"] == "feature/x"
    assert seen["params"]["ref"] == "feature/x"
    assert seen["params"]["event"] == "pull_request"
    body = resp.json()
    assert body["run_id"] == 42
    assert body["run_number"] == 7
    assert body["status"] == "success"
    # Forgejo field mapping
    assert body["display_title"] == "CI build"
    assert body["name"] == "CI build"
    assert body["branch"] == "feature/x"
    assert body["commit_sha"] == "abc123"
    assert body["started_at"] == "2026-07-16T10:00:00Z"
    assert body["updated_at"] == "2026-07-16T10:05:00Z"


# ─── Pipeline output: /jobs wrapper + per-job log routes ──────────────────


def test_pipeline_output_gitea29_jobs_and_job_logs(client, gitea_env):
    """GiteaGetPipelineOutput must understand the Gitea 29 jobs wrapper
    ({"jobs": [...], "total_count": N}) and find logs via the current
    /actions/jobs/{id}/logs route when the legacy one 404s."""
    with respx.mock(assert_all_called=False) as rx:
        rx.get(f"{GITEA_API}/repos/o/r/actions/runs/9").mock(
            return_value=httpx.Response(
                200,
                json={"id": 9, "status": "completed", "conclusion": "success", "display_title": "CI"},
            )
        )
        # Gitea 29 shape: wrapper with `jobs` key
        rx.get(f"{GITEA_API}/repos/o/r/actions/runs/9/jobs").mock(
            return_value=httpx.Response(
                200,
                json={
                    "total_count": 1,
                    "jobs": [
                        {
                            "id": 11,
                            "name": "build",
                            "status": "success",
                            "conclusion": "success",
                            "started_at": "2026-07-16T10:00:00Z",
                            "url": f"{GITEA_BASE}/o/r/actions/runs/9/jobs/11",
                        }
                    ],
                },
            )
        )
        # legacy per-job logs route is gone -> 404
        rx.get(f"{GITEA_API}/repos/o/r/actions/runs/9/jobs/11/logs").mock(
            return_value=httpx.Response(404, json={"message": "not found"})
        )
        # current per-job logs route -> text
        rx.get(f"{GITEA_API}/repos/o/r/actions/jobs/11/logs").mock(
            return_value=httpx.Response(
                200, content=b"step 1 ok\nstep 2 ok\n", headers={"Content-Type": "text/plain"}
            )
        )
        resp = client.get("/gitea/actions/runs/o/r/9/logs")
    assert resp.status_code == 200
    body = resp.json()
    assert body["run_id"] == 9
    assert [j["job_id"] for j in body["jobs"]] == [11]
    assert "step 1 ok" in body["logs"]


def test_pipeline_output_run_level_logs_fallback(client, gitea_env):
    """If no per-job log route works at all, fall back to run-level logs
    (available on current Gitea/Forgejo)."""
    with respx.mock(assert_all_called=False) as rx:
        rx.get(f"{GITEA_API}/repos/o/r/actions/runs/9").mock(
            return_value=httpx.Response(
                200, json={"id": 9, "status": "failed", "conclusion": "failure"}
            )
        )
        rx.get(f"{GITEA_API}/repos/o/r/actions/runs/9/jobs").mock(
            return_value=httpx.Response(
                200, json={"total_count": 1, "jobs": [{"id": 11, "name": "build", "status": "failure"}]}
            )
        )
        rx.get(f"{GITEA_API}/repos/o/r/actions/runs/9/jobs/11/logs").mock(
            return_value=httpx.Response(404, json={"message": "not found"})
        )
        rx.get(f"{GITEA_API}/repos/o/r/actions/jobs/11/logs").mock(
            return_value=httpx.Response(404, json={"message": "not found"})
        )
        rx.get(f"{GITEA_API}/repos/o/r/actions/runs/9/logs").mock(
            return_value=httpx.Response(
                200, content=b"run level log line\n", headers={"Content-Type": "text/plain"}
            )
        )
        resp = client.get("/gitea/actions/runs/o/r/9/logs")
    assert resp.status_code == 200
    assert "run level log line" in resp.json()["logs"]
