# Scope: Per-user identity for the context recall tools

Status: **implemented** (companion side; OWUI-side setup still one-time per instance) · Target: OWUI **v0.12.3** (verified against the `v0.12.3` tag, not `main`)

## Problem

All six `/context/*` endpoints authenticate to Open-WebUI with one static
`OWUI_TOKEN`. Every chat user's recall therefore runs as that token's
identity. Because OWUI's chat endpoints are **owner-only** (see below),
"cross-chat" recall with an admin token really means "the admin's own chats",
and per-user scoping is impossible without forwarding the caller's own OWUI
credentials.

## What OWUI v0.12.3 gives us (verified)

| Fact | Source (v0.12.3) |
| --- | --- |
| The tool-call pipeline builds tool-server headers **per invocation** with the *live* `request` and `user`: `get_tools(...)` → `make_tool_function` → `tool_function(**kwargs)` calls `build_tool_server_headers(connection, request, user, ...)` on every model tool call | `backend/open_webui/utils/tools.py` (`get_tools`), called from the `execute_tool_call` loop in `backend/open_webui/utils/middleware.py` |
| **`auth_type: 'session'`** sends `Authorization: Bearer <calling user's OWUI JWT>` (`request.state.token.credentials`) on every tool-server call | `build_tool_server_headers()` in `utils/tools.py`; UI option in `src/lib/components/AddToolServerModal.svelte` — *"Session — Forwards system user session credentials to authenticate"* — available for **OpenAPI** tool servers |
| `POST /chats/{id}/compact` exists and works in 0.12.3 (our module's target API is current) | `routers/chats.py` |
| `GET /chats/search` → **strictly the token owner's chats** (no admin bypass) | `search_user_chats` → `Chats.get_chats_by_user_id_and_search_text(user.id, ...)` |
| `POST /chats/{id}/compact` → **strictly the token owner** (`get_chat_by_id_and_user_id`, no admin bypass) | `compact_chat_by_id` |
| `GET /chats/{id}` → owner, *or* admin with `ENABLE_ADMIN_CHAT_ACCESS` | `get_chat_by_id` → `Chats.get_accessible_chat_by_id` |
| Token validation endpoint: `GET /api/v1/users/user/info` — JWT verified + user exists (`get_verified_user`); the response body is `user.info`, a **self-editable dict (typically `{}`)** — *not* an identity | `routers/users.py` (`get_user_info`) |
| Authoritative user attributes: `GET /api/v1/users/user/status` returns the **full user object** (`id`, `name`, `email`, `role`, …) for the calling user; 403 when disabled via `users.enable_status` | `routers/users.py` (`get_user_status`) |
| OWUI **session JWTs carry the user id in an `id` claim (not `sub`)** | `utils/auth.py` (`create_token` for session tokens) |
| Optional signed identity: with `ENABLE_FORWARD_USER_INFO_HEADERS=true` OWUI adds `X-OpenWebUI-Chat-Id`/`-Message-Id`, and either `X-OpenWebUI-User-Id/-Name/-Email/-Role` headers **or** a signed HS256 JWT in `X-OpenWebUI-User-Jwt` (claims: `sub` = user id, `email`, `name`, `role`, `iss: "open-webui"`; when `FORWARD_USER_INFO_HEADER_JWT_SECRET` is set, TTL default 300 s) | `utils/headers.py` (`include_user_info_headers`), env names in `env.py` |

**Consequence:** a static admin token *cannot* delegate to other users for
`/search` or `/compact` — the per-user JWT is mandatory, not an optimization.

## Design: token passthrough with validation + fail-closed static fallback

The companion uses **the JWT OWUI already sends us** (Session auth) for the
outbound OWUI API calls, after validating it against the OWUI backend.
**Chat-data endpoints (usage/compact/search) are fail-closed**: they
require a validated *forwarded* user token and never fall back to the
static `OWUI_TOKEN` (which would let anyone with network access read
chats as the admin). Only the diagnostic `/context/health` endpoint may
fall back; with `OWUI_ALLOW_USER_TOKEN=false` (explicit legacy mode)
everything runs as the static token.

### OWUI-side setup (one-time, admin)

1. Settings → Admin → Integrations → Tools → add our
   `http://<companion>:8090/openapi.json`.
2. **Authentication: Session** (no API key needed). Enable.
3. Optional (Phase 2): on the OWUI backend set
   `ENABLE_FORWARD_USER_INFO_HEADERS=true` and share
   `FORWARD_USER_INFO_HEADER_JWT_SECRET` with the companion for a signed
   cross-check.

### Companion-side changes

1. **`app/config.py`** (~15 lines)
   - `OWUI_ALLOW_USER_TOKEN: bool = True` (master switch, default on)
   - `OWUI_TOKEN_CACHE_TTL: int = 300` (matches OWUI's own forward-JWT TTL)
   - `OWUI_FORWARD_JWT_SECRET: str = ""` (Phase 2 cross-check key)
   - All int env parsing is tolerant (bad values fall back to defaults
     instead of crashing at import).
2. **`app/identity.py`** (new, ~340 lines)
   - `OwuiIdentity` **frozen** dataclass: `token, source, user_id, name,
     email, role` (`"forwarded" | "static"`) — cached instances can never
     be re-attributed by a later request.
   - `async _validate_token(token, source)` — the two-step validation:
     1. `GET {owui_api_url}/users/user/info` with `Bearer T` (10 s
        timeout) is the **validation authority**: 200 = valid, 401/403 =
        rejected (negative-cached 30 s), other/transport failure =
        `OWUIError` (distinguished from rejection; never cached).
     2. `GET {owui_api_url}/users/user/status` supplies the
        **authoritative attributes** (id/name/email/role). If it 403s
        (`users.enable_status` off) the identity stays valid with degraded
        attributes (user id from the token's unsigned `id` claim).
   - `async resolve_identity(request)` — diagnostic/legacy path:
     validated forwarded token → validated static `OWUI_TOKEN` →
     `ConfigError`. A *presented but rejected* forwarded token is a 401
     (no silent fallback to the static identity).
   - `async resolve_forwarded_identity(request)` — chat-data path:
     requires a validated forwarded token; **401 when absent or
     rejected; the static token never satisfies it**.
   - Validation cache: `sha256(token) → (timestamp, identity | rejected)`,
     TTL `OWUI_TOKEN_CACHE_TTL` (30 s negative), capped at 1024 entries
     (oldest evicted first). A token invalidated mid-TTL simply fails on
     the next OWUI call and the existing `OWUIError` mapping surfaces it.
   - Security model:
     - A forwarded JWT is the user's real OWUI credential; whoever holds it
       can already call the OWUI API directly → **no privilege escalation**
       by using it.
     - `X-OpenWebUI-User-*` headers are **never** trusted for auth
       (log/audit only) — they are plain and spoofable.
     - Phase 2 (fail-closed): when the shared secret is configured, a
       forwarded-token request **must** carry a valid signed
       `X-OpenWebUI-User-Jwt` (HS256, `iss: "open-webui"`); a missing or
       invalid header is a 401, so a MITM cannot strip the header to skip
       the check. For session JWTs, **either** a positive `sub` match
       (against the token's user id — the `id` claim pre-validation, the
       backend-confirmed id post-validation) **or** a positive `email`
       match suffices; a request is rejected only when no claim matches
       (the two claims are independent, so a mismatch on the other one is
       not additional signal). For `sk-` API keys only the header's
       authenticity is verified (it describes the calling user, not the
       key owner).
     - `/context/health` is unauthenticated, so its `identity` block
       **redacts static identities** (`{"source": "static", "redacted":
       true}`) — no email/role/user_id is leaked there.
3. **`app/context.py`** (~40 lines changed)
   - `_owui_request(method, path, **kwargs)` — per-identity
     `Authorization` header (pass `headers=identity.auth_header`);
     `OWUIError` carries the backend `status_code`.
   - All chat-data endpoints resolve identity **first** via
     `resolve_forwarded_identity` (fail-closed); `/context/health` via
     `resolve_identity`.
   - Input validation (model-generated args are untrusted): `limit ∈
     [1,100]`, `context_chars ∈ [10,4000]`, `page ≥ 1`, `text` non-empty,
     `chat_id` must be a UUID (also covers the
     `X-OpenWebUI-Chat-Id` header) before it reaches an outbound URL.
   - Tool-facing error shape: an OWUI failure for a specific chat returns
     HTTP 200 `{"chat_id": ..., "error": "..."}` so the model can read the
     problem (stated in the module docstring).
   - One structured log line per request: resolved user id/email + source
     (single `_log_tool` helper).
   - The admin-only `GET`/`POST /context/config` endpoints were **removed**
     (they 403'd for everyone against the v0.12.3 contract; the global
     compaction config is OWUI-admin territory, not a per-user tool).
4. **`README.md`** (~40 lines)
   - New "Per-user identity" section: Session auth setup, flow diagram in
     words, security model, behavior matrix, env-var table rows.
   - Update "Add the Tool to Open-WebUI": pick **Session** auth
     (Bearer + static key remains the legacy admin-only mode).
5. **No changes** to `Dockerfile` / `docker-compose.yml` (no new ports or
   required env), and **no OpenAPI spec changes** (operation IDs and
   signatures are untouched → existing OWUI tool registrations keep working).

### Behavior matrix

| Caller | Chat-data endpoints (usage/compact/search) | `/context/health` |
| --- | --- | --- |
| OWUI tool call (Session auth) | calling user's validated JWT | same |
| Direct call with a valid user JWT | that user (same scoping) | same |
| Direct call, no auth | **401** (fail-closed) | static `OWUI_TOKEN` (validated) |
| Presented token rejected by OWUI | **401** (never a silent static fallback) | `ConfigError` if only a static token exists and it is invalid |
| Legacy mode (`OWUI_ALLOW_USER_TOKEN=false`) | static `OWUI_TOKEN` for everything | same |
| No static token, no forwarded token | 401 | `ConfigError` |

### Testing

The suite exists and is wired into CI (`.github/workflows/docker.yml`
`test` job: `pip install -r requirements-dev.txt`, `ruff check app/ tests/`,
`pytest`):

- `tests/test_identity.py` (pytest + respx): validation against the **real
  v0.12.3 contract** (info dict body, full user from `/user/status`, JWT
  `id` claim), cache hits & negative caching, the 401 fail-closed rules,
  and the signed-header cross-check (match / sub mismatch / email mismatch
  / bad signature / wrong issuer / expired / missing header / API-key
  paths).
- `tests/test_context.py` (pytest + respx): the **v0.12.3 contract** —
  per-user header propagation, fail-closed static rules, single-chat
  branch reconstruction + local content search, OWUI server-side
  cross-chat search (`GET /chats/search`), server-reported
  `context_usage` passthrough (and the degraded no-usage shape),
  `dry_run` and model-override compact, input clamps (422s), UUID
  `chat_id` validation, OWUI 403/5xx graceful error dicts, redacted
  health, signed-header cross-check (mismatch / email-only match /
  missing header), and legacy mode.
- Manual integration script (documented in README, not CI): OWUI 0.12.3 in
  Docker + two users (regular + admin) → verify A's `ContextSearch` sees
  only A's chats, A compacting B's chat fails with a 403-backed error,
  direct curl with A's JWT behaves as A, no-auth call to a chat-data
  endpoint is a 401.

### Effort & risk

| Item | Estimate |
| --- | --- |
| Companion code (identity + threading + guards + README) | ~1 day |
| pytest suite | ~0.5 day |
| Live 0.12.3 integration verification | ~0.5 day |

Risk: **low**. Additive; the legacy path (opt-out) preserves current
behavior; no spec changes. One deliberate behavior change to document: with
Session auth the chat-data endpoints **fail closed** — a request without a
valid forwarded user token is a 401 instead of silently running as the
admin token.

## Open decisions

1. `ContextConfigGet` for non-admins: **decided — endpoints removed.**
   The admin-only `GET`/`POST /context/config` were broken for everyone
   against the v0.12.3 contract (the role came from the user-editable
   `user.info` dict and was always `"unknown"`); per review the fix is
   removal, not repair — global compaction config stays OWUI-admin
   territory.
2. Phase 2 scope: **decided — in scope and implemented.** The signed
   `X-OpenWebUI-User-Jwt` cross-check (active only when
   `OWUI_FORWARD_JWT_SECRET` is set) and `X-OpenWebUI-Chat-Id` used to
   default `chat_id` — **refinement:** the header default applies only to
   `ContextUsage` and `ContextCompact`, *not* `ContextSearch` (an omitted
   `chat_id` there is the explicit cross-chat search semantic).
3. Env var naming: **decided — `OWUI_ALLOW_USER_TOKEN`** (master switch,
   default on), plus `OWUI_TOKEN_CACHE_TTL` (default 300) and
   `OWUI_FORWARD_JWT_SECRET` (default empty) — all `OWUI_`-prefixed,
   consistent with the existing settings.
