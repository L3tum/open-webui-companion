"""Main FastAPI application for the Open-WebUI companion server.

This server provides tools for Open-WebUI including:
- Evidence management: Store and retrieve facts to reduce hallucinations
- Gitea integration: Repository management, file operations, issues, PRs, and comments
- Notes sync: Sync Open-WebUI notes into a Knowledge Base for RAG indexing
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.errors import add_error_handlers
from app.logging import get_logger, setup_logging
from app.evidence import router as evidence_router
from app.gitea import router as gitea_router
from app.notes_sync import router as notes_sync_router
from app.scheduler import setup_scheduler, shutdown_scheduler

# Setup structured logging first
setup_logging(level=settings.LOG_LEVEL, structured=settings.LOG_STRUCTURED)
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler for startup/shutdown events."""
    # Startup
    logger.info(
        "Starting Open-WebUI Companion Server",
        host=settings.HOST,
        port=settings.PORT,
        gitea_configured=settings.is_gitea_configured(),
        owui_configured=settings.is_owui_configured(),
    )

    # Start background scheduler
    setup_scheduler()

    yield

    # Shutdown
    shutdown_scheduler()
    logger.info("Shutting down Open-WebUI Companion Server")


# Create the FastAPI application
app = FastAPI(
    title="Open-WebUI Companion Server",
    description="""
A companion server for Open-WebUI that provides persistent tools for:

## Evidence Management
- **EvidenceAdd**: Store verified facts, sources, or findings
- **EvidenceList**: Retrieve stored evidence (with optional tag filtering)
- **EvidenceGet**: Get a specific evidence item by ID
- **EvidenceUpdate**: Update an existing evidence item
- **EvidenceDelete**: Delete an evidence item
- **EvidenceClear**: Clear all evidence (use with caution)

## Gitea Integration
- **GiteaCreateRepo**: Create a new repository
- **GiteaListRepos**: List your repositories
- **GiteaGetRepo**: Get repository details
- **GiteaCreateFile**: Create or update a file
- **GiteaGetFile**: Get file contents
- **GiteaListFiles**: List files in a directory
- **GiteaCreateIssue**: Create an issue
- **GiteaListIssues**: List issues
- **GiteaCreatePR**: Create a pull request
- **GiteaListPRs**: List pull requests
- **GiteaPostComment**: Post a comment on an issue or PR
- **GiteaListComments**: List comments on an issue or PR
- **GiteaGetPRPipeline**: Get pipeline status for a PR (check if CI passed)
- **GiteaGetPipelineOutput**: Get full pipeline logs for a workflow run
- **GiteaHealth**: Check Gitea connection

## Notes Sync
- **NotesSyncTrigger**: Manually sync notes to a Knowledge Base
- **NotesSyncStatus**: Get sync status
- **NotesSyncHealth**: Check Open-WebUI connection

## System Prompt Recommendations
Add this to your Open-WebUI model's system prompt:

> You have access to evidence management, Gitea tools, and notes sync.
> - Before answering factual questions, check EvidenceList for stored evidence.
> - When the user provides facts or sources, use EvidenceAdd to store them.
> - Use Gitea tools to create repos, edit files, create PRs, and comment on issues/PRs.
> - After creating or modifying a PR, use GiteaGetPRPipeline to check if CI passed.
> - If a pipeline fails, use GiteaGetPipelineOutput to fetch the logs and diagnose the issue.
> - Always confirm with the user before making changes on Gitea.
> - Use NotesSyncTrigger to sync your notes into RAG when needed.
    """,
    version="1.0.0",
    lifespan=lifespan,
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Restrict in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Add global error handlers
add_error_handlers(app)

# Include routers
app.include_router(evidence_router)
app.include_router(gitea_router)
app.include_router(notes_sync_router)


@app.get("/health", tags=["system"])
async def health_check():
    """Health check endpoint for monitoring."""
    return {
        "status": "ok",
        "service": "openwebui-companion",
        "version": "1.0.0",
        "gitea_configured": settings.is_gitea_configured(),
        "owui_configured": settings.is_owui_configured(),
    }


@app.get("/", tags=["system"])
async def root():
    """Root endpoint with API documentation links."""
    return {
        "message": "Open-WebUI Companion Server",
        "docs": "/docs",
        "openapi": "/openapi.json",
        "health": "/health",
    }
