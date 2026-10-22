"""
Open-WebUI Companion Server

A FastAPI-based companion server for Open-WebUI that provides:
- Gitea integration tools (repos, files, issues, PRs, pipelines)
- Context management tools (usage inspection, manual compaction,
  cross-chat history search)
- Company / website vetting tools (deterministic anti-scam checks
  with minimal search-engine use)

The server runs on port 8090 by default and can be registered in
Open-WebUI via the OpenAPI specification.
"""

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.context import router as context_router
from app.errors import add_error_handlers
from app.gitea import router as gitea_router
from app.logging import setup_logging
from app.owui import OwuiClient
from app.vetting import router as vetting_router

setup_logging(settings.LOG_LEVEL)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan: manage the shared Open-WebUI client."""
    app.state.owui_client = OwuiClient(base_url=settings.OWUI_INSTANCE_URL)
    yield
    await app.state.owui_client.aclose()


app = FastAPI(
    title="Open-WebUI Companion Server",
    description="""
A companion server for Open-WebUI providing persistent tools:

## Gitea Integration
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

## Context Compaction
Inspect a chat's model context, manually compact it, and *recall*
dropped detail by searching the full un-compacted history (within one
chat or across all chats). All chat-data calls act as the validated
calling user (fail-closed).
- **ContextUsage**: Inspect a chat's model context window usage
- **ContextCompact**: Manually compact a chat's context
- **ContextSearch**: Search a chat's (or all accessible chats') full history
- **ContextHealth**: Check OWUI connection and resolved identity

## Company Vetting
Deterministic anti-scam checks before recommending a company/website.
Almost all checks are direct lookups (RDAP, the website itself, the
Wayback Machine); the single optional search-engine query can be
disabled via `VET_ENABLE_MENTIONS=false`.
- **VetCompany**: Vet a company/website -> verdict pass/flag/reject
- **VetHealth**: Check vetting dependencies

In the System Prompt Recommendations section:
> - Use VetCompany before recommending any company/website. Verdict
>   `reject` -> do not recommend it, tell the user why, and offer
>   alternatives. Verdict `flag` -> recommend only as explicitly
>   unverified.

In the Tool Usage section:
> - Use Gitea tools to create repositories, edit files, manage issues, and review pull requests.
> - Always confirm with the user before making changes on Gitea.
> - Use GiteaCreateFile to scaffold new projects or update existing code.
> - Check pipeline status with GiteaGetPRPipeline after creating or updating PRs.
    """,
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

add_error_handlers(app)

app.include_router(context_router)
app.include_router(gitea_router)
app.include_router(vetting_router)


@app.get("/", tags=["Health"])
async def root() -> dict:
    """API root with links to documentation."""
    return {
        "service": "open-webui-companion",
        "version": "0.1.0",
        "docs": "/docs",
        "openapi": "/openapi.json",
    }


@app.get("/health", tags=["Health"], operation_id="HealthCheck")
async def health_check() -> dict:
    """Health check endpoint for monitoring."""
    return {
        "status": "healthy",
        "service": "open-webui-companion",
    }
