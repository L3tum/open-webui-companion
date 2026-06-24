# Open-WebUI Companion Server

## NOTICE

I had some bigger goals with this but decided to switch away and build something else. As such the functionality here is basically the same as Forgejo-MCP or Gitea-MCP projects.


A FastAPI-based companion server for [Open-WebUI](https://github.com/open-webui/open-webui) that provides persistent tools for evidence management, Gitea integration, and Open-WebUI notes synchronization.

## Features

### Evidence Management
- **EvidenceAdd**: Store verified facts, sources, or findings
- **EvidenceList**: Retrieve stored evidence with optional tag filtering
- **EvidenceGet**: Get a specific evidence item by ID
- **EvidenceUpdate**: Update an existing evidence item
- **EvidenceDelete**: Delete an evidence item
- **EvidenceClear**: Clear all evidence (use with caution)

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

### Open-WebUI Notes Sync
- **NotesSyncTrigger**: Manually trigger a sync of Open-WebUI notes into a Knowledge Base
- **NotesSyncStatus**: Get the current status of the notes sync
- **NotesSyncHealth**: Check if Open-WebUI is configured and accessible
- **NotesSyncDebug**: Test all known notes API endpoints and return raw responses

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
4. Save

The tools will now be available to any model that supports function calling.

### System Prompt

Add this to your model's system prompt for best results:

```
You have access to evidence management, Gitea, and Open-WebUI notes sync tools.

Evidence Management:
- Before answering factual questions, check EvidenceList for stored evidence.
- When the user provides facts, sources, or findings they want remembered, use EvidenceAdd to store them with relevant tags.
- Evidence persists across conversations to reduce hallucinations.

Gitea Integration:
- Use Gitea tools to create repositories, edit files, manage issues, and review pull requests.
- Always confirm with the user before making changes on Gitea.
- Use GiteaCreateFile to scaffold new projects or update existing code.
- Check pipeline status with GiteaGetPRPipeline after creating or updating PRs.

Open-WebUI Notes Sync:
- Use NotesSyncTrigger to sync user notes into a Knowledge Base.
- Check NotesSyncStatus to see if a sync is in progress.
```

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
| `EVIDENCE_DIR` | Directory for evidence storage | `/data` |

## Data Persistence

Evidence data is stored in a JSON file (`evidence.json`) in the `/data` directory. When using Docker, this is mounted as a volume for persistence.

## Security Notes

- **CORS**: The server allows all origins by default. Restrict this in production.
- **Gitea Token**: Store your Gitea token securely in the `.env` file.
- **Authentication**: Consider adding authentication if exposing the server publicly.

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
│   ├── evidence.py      # Evidence endpoints
│   ├── gitea.py         # Gitea endpoints
│   ├── notes_sync.py    # Open-WebUI notes sync endpoints
│   └── scheduler.py     # Background task scheduler
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