# Open-WebUI Companion Server

## NOTICE

I had some bigger goals with this but decided to switch away and build something else. As such the functionality here is basically the same as Forgejo-MCP or Gitea-MCP projects.


A FastAPI-based companion server for [Open-WebUI](https://github.com/open-webui/open-webui) that provides persistent tools for Gitea integration and chat context inspection, compaction, and recall.

## Features

### Gitea Integration
- **GiteaCreateRepo**: Create a new repository
- **GiteaListRepos**: List your repositories
- **GiteaGetRepo**: Get repository details
- **GiteaCreateFile**: Create or update a file
- **GiteaGetFile**: Get file contents
- **GiteaListFiles**: List files in a directory
- **GiteaCreateIssue**: Create an issue
- **GiteaListIssues**: List issues
- **GiteaUpdateIssue**: Update an issue's title and/or description
- **GiteaCreatePR**: Create a pull request
- **GiteaListPRs**: List pull requests
- **GiteaUpdatePR**: Update a pull request's title and/or description
- **GiteaPostComment**: Post a comment on an issue or PR
- **GiteaListComments**: List comments on an issue or PR
- **GiteaDeleteComment**: Delete a comment
- **GiteaGetPRDiff**: Get a PR's diff
- **GiteaGetPRFiles**: Get changed files in a PR
- **GiteaListPRComments**: List PR review comments
- **GiteaSubmitPRReview**: Submit a PR review
- **GiteaGetPRSummary**: Get a PR summary
- **GiteaGetPRPipeline**: Get pipeline status for a PR
- **GiteaGetPipelineOutput**: Get pipeline output/logs
- **GiteaGetToken**: Get the configured Gitea API token
- **GiteaHealth**: Check Gitea connection

### Context Compaction
Inspect a chat's model context, manually compact it, and *recall* dropped
detail by searching the full un-compacted history (within one chat or
across all chats). Uses OWUI_INSTANCE_URL / OWUI_TOKEN; chat-data calls
act as the validated calling user (see
[Per-user identity](#per-user-identity)).
- **ContextUsage**: Inspect a chat's model context window usage
- **ContextCompact**: Manually compact a chat's context
- **ContextSearch**: Search a chat's (or all accessible chats') full history
- **ContextHealth**: Check OWUI connection and resolved identity

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
You have access to Gitea tools.

Gitea Integration:
- Use Gitea tools to create repositories, edit files, manage issues, and review pull requests.
- Always confirm with the user before making changes on Gitea.
- Use GiteaCreateFile to scaffold new projects or update existing code.
- Check pipeline status with GiteaGetPRPipeline after creating or updating PRs.
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