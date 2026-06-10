# Open-WebUI Companion Server

A FastAPI-based companion server for [Open-WebUI](https://github.com/open-webui/open-webui) that provides persistent tools for evidence management and Gitea integration.

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
- **GiteaCreateIssue**: Create an issue
- **GiteaListIssues**: List issues
- **GiteaHealth**: Check Gitea connection

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
You have access to evidence management and Gitea tools.

Evidence Management:
- Before answering factual questions, check EvidenceList for stored evidence.
- When the user provides facts, sources, or findings they want remembered, use EvidenceAdd to store them with relevant tags.
- Evidence persists across conversations to reduce hallucinations.

Gitea Integration:
- Use Gitea tools to create repositories, edit files, and manage issues.
- Always confirm with the user before making changes on Gitea.
- Use GiteaCreateFile to scaffold new projects or update existing code.
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
│   ├── evidence.py      # Evidence endpoints
│   └── gitea.py         # Gitea endpoints
├── tests/
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
