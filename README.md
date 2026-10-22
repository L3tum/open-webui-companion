# Open-WebUI Companion Server

## NOTICE

I had some bigger goals with this but decided to switch away and build something else. As such the functionality here is basically the same as Forgejo-MCP or Gitea-MCP projects.


A FastAPI-based companion server for [Open-WebUI](https://github.com/open-webui/open-webui) that provides persistent tools for Gitea integration, chat context inspection/compaction/recall, and deterministic company/website vetting.

## Features

### Gitea Integration
- **GiteaCreateRepo**: Create a new repository
- **GiteaListRepos**: List your repositories
- **GiteaSearchRepos**: Find a repository instance-wide by name, owner or description
- **GiteaGetRepo**: Get repository details
- **GiteaCreateFile**: Create or update a file
- **GiteaGetFile**: Get file contents
- **GiteaListFiles**: List files in a directory
- **GiteaCreateIssue**: Create an issue
- **GiteaListIssues**: List issues
- **GiteaUpdateIssue**: Update an issue's title/description
- **GiteaCreatePR**: Create a pull request
- **GiteaListPRs**: List pull requests
- **GiteaUpdatePR**: Update a PR's title/description
- **GiteaPostComment**: Post a comment on an issue or PR
- **GiteaListComments**: List comments on an issue or PR
- **GiteaDeleteComment**: Delete a comment
- **GiteaGetPRDiff**: Get the full diff of a PR
- **GiteaGetPRFiles**: List files changed in a PR
- **GiteaListPRComments**: List review comments on a PR
- **GiteaSubmitPRReview**: Submit a review on a PR
- **GiteaGetPRSummary**: Get a structured PR summary
- **GiteaGetPRPipeline**: Check PR pipeline status
- **GiteaGetPipelineOutput**: Get pipeline output and logs
- **GiteaGetToken**: Get the configured Gitea API token
- **GiteaHealth**: Check Gitea connection

### Context Compaction
Inspect a chat's model context, manually compact it, and *recall*
dropped detail by searching the full un-compacted history (within one
chat or across all chats). Uses OWUI_INSTANCE_URL / OWUI_TOKEN; chat-data
calls act as the validated calling user (see
[Per-user identity](#per-user-identity)).
- **ContextUsage**: Inspect a chat's model context window usage
- **ContextCompact**: Manually compact a chat's context
- **ContextSearch**: Search a chat's (or all accessible chats') full history
- **ContextHealth**: Check OWUI connection and resolved identity

### Company Vetting
Deterministic anti-scam checks for a company name + website, designed to
keep search-engine use minimal — almost everything is a direct lookup
(RDAP registry data, the website itself, the Wayback Machine), and the
single optional exact-name search is cached with the verdict and can be
disabled entirely.
- **VetCompany**: Vet a company/website -> verdict `pass`/`flag`/`reject` with per-check evidence
- **VetHealth**: Check vetting dependency availability
- **VetCacheList**: List cached verdicts (domain, verdict, reasoning, expiry) from the persistent SQLite cache
- **VetCacheClear**: Clear the persistent + in-process verdict cache

Checks:
- **rdap** — domain age + registrar country. RDAP base URLs come from the
  IANA bootstrap file (`data.iana.org/rdap/dns.json`); registries without
  a resolvable registration date (e.g. DENIC/.de, GDPR-redacted) fall
  back to the first Wayback Machine capture as an age lower bound.
- **impressum** — German Impressum extraction (§ 5 DDG): postal
  address/PLZ, phone format, HRB registry numbers, VAT id, legal form,
  plus consistency (e.g. claims "GmbH" but lists no registry number).
- **language** — per-paragraph language fingerprint (langdetect); a
  mid-document language switch is flagged as an LLM-generation marker.
- **mentions** — ONE exact-name search via ddgs, counting *third-party*
  domains only (the company's own domain excluded). Disabled with
  `VET_ENABLE_MENTIONS=false`; a failed search is treated as
  inconclusive, never as "zero mentions".
- **registry** — derived from the Impressum by default; a live lookup can
  be configured with `VET_REGISTRY_URL_TEMPLATE` when you have a direct,
  machine-readable registry API (handelsregister.de is a stateful JS app
  and not plain-GET queryable).

Caching: verdicts are cached per name + domain, first in-process
(`VET_CACHE_TTL`, default 24 h) and then persistently in a local SQLite
database (`VET_CACHE_DB_PATH`, default `data/vetting.db`,
`VET_CACHE_DB_TTL`, default 30 days), so results survive restarts. Each
entry stores the verdict, the reasoning (summary + full per-check
evidence) and its expiry, and can be inspected or cleared via
`VetCacheList` / `VetCacheClear`.

> **System prompt**: add "Use VetCompany before recommending any
> company/website. Verdict reject -> do not recommend, say why, and
> offer alternatives; verdict flag -> recommend only as explicitly
> unverified."

### Health
- **HealthCheck**: Health check endpoint for monitoring
- **Root**: Root endpoint with API documentation links

## Quick Start

### 1. Clone and Configure

```bash
# Clone the repository
git clone <your-repo-url>
cd openwebui-companion-server

# Copy and edit the environment file
cp .env.example .env
# Edit .env with your Gitea credentials
```

### 2. Run with Docker (Recommended)

```bash
docker compose up -d
```

The server will be available at `http://localhost:8090`.

### 3. Run Locally (Development)

```bash
# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Run the server
uvicorn app.main:app --host 0.0.0.0 --port 8090 --reload
```

## Open-WebUI Integration

### Add the Tool to Open-WebUI

1. Go to **Admin Settings → Tools** in Open-WebUI
2. Click **Add Tool** and select **OpenAPI**
3. Enter the URL: `http://your-server-ip:8090/openapi.json`
4. Set **Authentication type** to **Session** — this forwards the calling
   user's JWT on every tool call, so each call acts as that user (see
   [Per-user identity](#per-user-identity))
5. Save

The tools will now be available to any model that supports function calling.

> **Legacy mode:** Bearer authentication with a static Open-WebUI API key
> still works, but every call then runs as that key's user (typically an
> admin).

### System Prompt

Add this to your model's system prompt for best results:

```
You have access to Gitea tools and a company vetting tool.

Gitea Integration:
- Use Gitea tools to create repositories, edit files, manage issues, and review pull requests.
- Always confirm with the user before making changes on Gitea.
- Use GiteaCreateFile to scaffold new projects or update existing code.
- Check pipeline status with GiteaGetPRPipeline after creating or updating PRs.

Company Vetting:
- Use VetCompany before recommending any company/website.
- Verdict reject -> do not recommend, say why, and offer alternatives.
- Verdict flag -> recommend only as explicitly unverified.
```

## Per-user identity

With **Session** authentication, Open-WebUI forwards the *calling user's*
JWT to this server on every tool call. The companion validates that token
against the Open-WebUI backend (`GET /users/user/info` — the validation
authority; result cached for `OWUI_TOKEN_CACHE_TTL` seconds), reads the
user's authoritative attributes from `GET /users/user/status`, and then
makes all Open-WebUI API calls *as that user* — so `ContextSearch` and
`ContextCompact` are strictly scoped to the caller's own chats.

The chat-data endpoints are **fail-closed**: a request without a valid
forwarded user token gets a **401** — there is no silent fallback to the
static `OWUI_TOKEN` (which would let anyone with network access read
chats as the admin). Only the diagnostic `ContextHealth` endpoint may use
the static token; with `OWUI_ALLOW_USER_TOKEN=false` (explicit legacy
mode) everything runs as the static token. Plain `X-OpenWebUI-User-*`
headers are never trusted for auth; when a shared secret is configured,
the signed `X-OpenWebUI-User-Jwt` is verified as a cross-check.

| Caller | Chat-data endpoints | `ContextHealth` |
| --- | --- | --- |
| OWUI tool call (Session auth) | calling user's validated JWT | same |
| OWUI tool call (Bearer + admin key, legacy mode) | that key's user | same |
| Direct call with a user's JWT | that user | same |
| Direct call, no auth | **401** | static `OWUI_TOKEN` |
| Presented token rejected by OWUI | **401** (never a silent static fallback) | `ConfigError` if the static token is also invalid |

### Optional: signed identity + current chat (OWUI side)

On the Open-WebUI backend (env / admin settings), you can additionally set:

- `ENABLE_FORWARD_USER_INFO_HEADERS=true` — OWUI then sends
  `X-OpenWebUI-Chat-Id` (the current chat) with every tool call. **This
  lets the model omit `chat_id` from `ContextUsage` and `ContextCompact`
  — the current chat is used automatically.** `ContextSearch` is
  intentionally unaffected: an omitted `chat_id` there means a
  cross-chat search.
- `FORWARD_USER_INFO_HEADER_JWT_SECRET` — when set, OWUI signs the
  forwarded identity into `X-OpenWebUI-User-Jwt` (HS256, ~5 min TTL).
  Set the companion's `OWUI_FORWARD_JWT_SECRET` to the **same value** to
  enable the signed cross-check. The check is **fail-closed**: once the
  secret is configured, a forwarded-token request *must* carry a valid
  header, and **either** its `sub` matching the token's user id **or**
  its `email` matching the token's email is accepted (no claim match →
  **401**). A missing, expired, or forged header is a **401** — so
  stripping the header never skips the check.

### Removed: admin-only config endpoints

`GET`/`POST /context/config` were removed: they read the caller's role
from the user-editable `user.info` dict and 403'd for *every* user
(including admins) against the v0.12.3 contract. Global compaction
configuration (`context_messages`/`context_tokens` defaults) stays in
Open-WebUI's admin settings, not in the per-user tool surface.

## API Documentation

Once running, visit:
- **Swagger UI**: `http://localhost:8090/docs`
- **ReDoc**: `http://localhost:8090/redoc`
- **OpenAPI JSON**: `http://localhost:8090/openapi.json`

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `GITEA_INSTANCE_URL` | Your Gitea instance URL | `https://gitea.com` |
| `GITEA_TOKEN` | Personal access token | (required for Gitea tools) |
| `GITEA_USERNAME` | Your Gitea username | (optional) |
| `HOST` | Server host | `0.0.0.0` |
| `PORT` | Server port | `8090` |
| `LOG_LEVEL` | Logging level | `info` |
| `OWUI_INSTANCE_URL` | Open-WebUI instance URL (e.g. `http://localhost:8080`) | (required for OWUI tools) |
| `OWUI_TOKEN` | Static Open-WebUI token (admin/API key) — fallback for `ContextHealth` and legacy mode only | (required for legacy mode) |
| `OWUI_ALLOW_USER_TOKEN` | Accept the forwarded user JWT (Session auth) as the acting identity | `true` |
| `OWUI_TOKEN_CACHE_TTL` | Validation cache TTL (seconds) for forwarded/user tokens | `300` |
| `OWUI_FORWARD_JWT_SECRET` | Shared secret to verify the signed `X-OpenWebUI-User-Jwt` header (empty = cross-check off) | (empty) |
| `VET_ENABLE_MENTIONS` | Enable the one optional search-engine query (third-party mentions) | `true` |
| `VET_MENTION_MAX_RESULTS` | Max results for the mention check | `10` |
| `VET_MIN_DOMAIN_AGE_DAYS` | Domain age (days) below which the domain is flagged | `90` |
| `VET_FOREIGN_REGISTRAR_MAX_AGE_DAYS` | Max domain age (days) for a foreign-registrar *rejection* (vs. flag) | `180` |
| `VET_CACHE_TTL` | Seconds to reuse an identical verdict (same name + domain, in-process tier) | `86400` |
| `VET_CACHE_DB_ENABLED` | Enable the persistent SQLite verdict cache | `true` |
| `VET_CACHE_DB_PATH` | SQLite database path for the verdict cache | `data/vetting.db` |
| `VET_CACHE_DB_TTL` | Seconds a cached verdict stays valid in the persistent tier | `2592000` (30 days) |
| `VET_HTTP_TIMEOUT` | Per-request timeout (seconds) for vetting lookups | `20.0` |
| `VET_REGISTRY_URL_TEMPLATE` | Direct registry lookup URL with `{query}` placeholder (empty = Impressum-derived check) | (empty) |
| `VET_USER_AGENT` | User agent for vetting lookups | `open-webui-companion-vetting/1.0` |

## Security Notes

- **CORS**: The server allows all origins by default. Restrict this in production.
- **Gitea Token**: Store your Gitea token securely in the `.env` file.
- **Authentication**: Consider adding authentication if exposing the server publicly.
- **Default deployment exposure**: the server is usually published on
  port 8090, and Open-WebUI's `ENABLE_ADMIN_CHAT_ACCESS` defaults to
  true. The chat-data endpoints therefore require a valid forwarded OWUI
  **user** token (fail-closed 401 without one), but `ContextHealth`
  still reports identity details using the static `OWUI_TOKEN` when no
  token is forwarded. If the port is reachable beyond your trusted
  network, restrict it (firewall / reverse proxy) or set
  `OWUI_ALLOW_USER_TOKEN=false` and protect the static token tightly —
  it grants access to its owner's chats through this server. `ContextHealth`
  is unauthenticated: it validates the static token when no token is
  forwarded, but the resolved static identity is **redacted** in the
  response (`{"source": "static", "redacted": true}`) so no
  email/role/user_id is leaked.

## Development

### Project Structure

```
openwebui-companion-server/
├── app/
│   ├── __init__.py
│   ├── main.py          # FastAPI application
│   ├── config.py        # Configuration management
│   ├── errors.py        # Error handling
│   ├── logging.py       # Logging configuration
│   ├── context.py       # Open-WebUI context compaction + recall endpoints
│   ├── identity.py      # Per-user identity resolution (forwarded JWT + static fallback)
│   ├── gitea.py         # Gitea endpoints
│   ├── vetting.py       # Company/website vetting endpoints
│   └── owui.py          # Shared Open-WebUI API client
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env.example
└── README.md
```

### Adding New Tools

1. Create a new module in `app/` (e.g., `app/weather.py`)
2. Define your endpoints with proper OpenAPI documentation
3. Include the router in `app/main.py`:
   ```python
   from app.weather import router as weather_router

   app.include_router(weather_router)
   ```

## License

MIT
