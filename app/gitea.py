"""Gitea integration endpoints for Open-WebUI.

Provides tools for repository management, file operations, issue tracking,
pull requests, and commenting via the Gitea REST API.
"""

import base64
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.config import settings
from app.errors import GiteaError
from app.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/gitea", tags=["gitea"])


class RepoCreate(BaseModel):
    """Schema for creating a new repository."""

    name: str = Field(..., description="Repository name")
    description: str = Field(default="", description="Repository description")
    private: bool = Field(default=True, description="Whether the repo is private")
    auto_init: bool = Field(default=True, description="Initialize with README")


class FileCreate(BaseModel):
    """Schema for creating or updating a file."""

    owner: str = Field(..., description="Repository owner (username or org)")
    repo: str = Field(..., description="Repository name")
    path: str = Field(..., description="File path within the repo")
    content: str = Field(..., description="File content")
    message: str = Field(default="Add file via companion server", description="Commit message")
    branch: str = Field(default="", description="Branch name (uses default if empty)")


class IssueCreate(BaseModel):
    """Schema for creating an issue."""

    owner: str = Field(..., description="Repository owner")
    repo: str = Field(..., description="Repository name")
    title: str = Field(..., description="Issue title")
    body: str = Field(default="", description="Issue description")
    labels: list[str] = Field(default_factory=list, description="Label names")


class PRCreate(BaseModel):
    """Schema for creating a pull request."""

    owner: str = Field(..., description="Repository owner")
    repo: str = Field(..., description="Repository name")
    title: str = Field(..., description="PR title")
    body: str = Field(default="", description="PR description")
    head: str = Field(..., description="Source branch name")
    base: str = Field(..., description="Target branch name")
    draft: bool = Field(default=False, description="Create as draft PR")


class CommentCreate(BaseModel):
    """Schema for creating a comment on an issue or PR."""

    owner: str = Field(..., description="Repository owner")
    repo: str = Field(..., description="Repository name")
    issue_index: int = Field(..., description="Issue or PR index number")
    body: str = Field(..., description="Comment text")


async def _gitea_request(method: str, url: str, **kwargs) -> dict:
    """Make a request to the Gitea API with error handling."""
    settings.require_gitea_config()

    kwargs.setdefault("headers", settings.gitea_headers)

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.request(method, url, **kwargs)
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as e:
        error_body = e.response.text[:500]
        logger.error(
            "Gitea API request failed",
            method=method,
            url=str(url),
            status_code=e.response.status_code,
            response=error_body,
        )
        raise GiteaError(
            f"Gitea API error {e.response.status_code}: {error_body}",
            status_code=e.response.status_code,
        ) from e
    except httpx.RequestError as e:
        logger.error("Gitea API request error", method=method, url=str(url), error=str(e))
        raise GiteaError(f"Failed to connect to Gitea: {e}") from e


@router.post("/repos", summary="Create a new repository", operation_id="GiteaCreateRepo")
async def create_repo(repo: RepoCreate) -> dict:
    """
    Create a new repository on Gitea.

    Creates a new repository with the specified name and settings.
    Optionally initializes it with a README file.
    """
    logger.info("Creating repository", name=repo.name, private=repo.private)

    result = await _gitea_request(
        "POST",
        f"{settings.gitea_api_url}/user/repos",
        json={
            "name": repo.name,
            "description": repo.description,
            "private": repo.private,
            "auto_init": repo.auto_init,
        },
    )

    logger.info("Repository created", name=repo.name, clone_url=result.get("clone_url"))
    return result


@router.get("/repos", summary="List repositories", operation_id="GiteaListRepos")
async def list_repos(
    page: int = Query(default=1, ge=1, description="Page number"),
    limit: int = Query(default=30, ge=1, le=100, description="Items per page"),
) -> list:
    """List repositories accessible to the authenticated user."""
    return await _gitea_request(
        "GET",
        f"{settings.gitea_api_url}/user/repos",
        params={"page": page, "limit": limit},
    )


@router.get("/repos/{owner}/{repo}", summary="Get repository details", operation_id="GiteaGetRepo")
async def get_repo(owner: str, repo: str) -> dict:
    """Get details of a specific repository."""
    return await _gitea_request(
        "GET",
        f"{settings.gitea_api_url}/repos/{owner}/{repo}",
    )


@router.post("/files", summary="Create or update a file", operation_id="GiteaCreateFile")
async def create_file(file: FileCreate) -> dict:
    """
    Create or update a file in a repository.

    Creates a new file or updates an existing one with the provided content.
    For updates, the existing file's SHA is fetched first to satisfy the API requirement.
    """
    logger.info(
        "Creating/updating file",
        owner=file.owner,
        repo=file.repo,
        path=file.path,
        branch=file.branch or "default",
    )

    encoded_content = base64.b64encode(file.content.encode()).decode()
    payload = {"content": encoded_content, "message": file.message}

    if file.branch:
        payload["branch"] = file.branch

    # Check if the file already exists — if so, we need its SHA for the update
    ref = file.branch if file.branch else None
    try:
        existing = await _gitea_request(
            "GET",
            f"{settings.gitea_api_url}/repos/{file.owner}/{file.repo}/contents/{file.path}",
            params={"ref": ref} if ref else {},
        )
        payload["sha"] = existing["sha"]
        logger.info(
            "File exists, including SHA for update",
            owner=file.owner,
            repo=file.repo,
            path=file.path,
            sha=payload["sha"][:12],
        )
    except GiteaError as e:
        if e.status_code == 404:
            logger.info(
                "File does not exist, creating new",
                owner=file.owner,
                repo=file.repo,
                path=file.path,
            )
        else:
            logger.warning(
                "Unexpected error checking for existing file",
                owner=file.owner,
                repo=file.repo,
                path=file.path,
                error=str(e),
            )
            # Proceed without SHA — will succeed if file doesn't exist,
            # fail with 422 if it does (and the error will be clear)

    result = await _gitea_request(
        "PUT",
        f"{settings.gitea_api_url}/repos/{file.owner}/{file.repo}/contents/{file.path}",
        json=payload,
    )

    logger.info("File written", owner=file.owner, repo=file.repo, path=file.path)
    return result


@router.get("/files/{owner}/{repo}/{path:path}", summary="Get file contents", operation_id="GiteaGetFile")
async def get_file(
    owner: str,
    repo: str,
    path: str,
    branch: str = Query(default="", description="Branch name"),
) -> str:
    """
    Get the contents of a file from a repository.

    Returns the decoded file content as a string.
    If the path is a directory, returns a formatted listing of its contents.
    """
    params = {"ref": branch} if branch else {}

    response = await _gitea_request(
        "GET",
        f"{settings.gitea_api_url}/repos/{owner}/{repo}/contents/{path}",
        params=params,
    )

    # Handle directory listing (API returns a list for directories)
    if isinstance(response, list):
        logger.info(
            "Path is a directory, returning listing",
            owner=owner,
            repo=repo,
            path=path,
            entries=len(response),
        )
        lines = [f"Directory listing for `{path}` ({len(response)} entries):\n"]
        for entry in response:
            name = entry.get("name", "unknown")
            entry_type = "📁" if entry.get("type") == "dir" else "📄"
            lines.append(f"  {entry_type} {name}")
        return "\n".join(lines)

    # Decode the base64 content (single file)
    content = response.get("content", "")
    encoding = response.get("encoding", "base64")

    if encoding == "base64":
        return base64.b64decode(content).decode("utf-8")
    return content


@router.post("/issues", summary="Create an issue", operation_id="GiteaCreateIssue")
async def create_issue(issue: IssueCreate) -> dict:
    """Create a new issue in a repository."""
    logger.info("Creating issue", owner=issue.owner, repo=issue.repo, title=issue.title)

    result = await _gitea_request(
        "POST",
        f"{settings.gitea_api_url}/repos/{issue.owner}/{issue.repo}/issues",
        json={
            "title": issue.title,
            "body": issue.body,
            "labels": issue.labels,
        },
    )

    logger.info("Issue created", owner=issue.owner, repo=issue.repo, index=result.get("number"))
    return result


@router.get("/issues/{owner}/{repo}", summary="List issues", operation_id="GiteaListIssues")
async def list_issues(
    owner: str,
    repo: str,
    state: str = Query(default="open", description="Issue state: open, closed, or all"),
    page: int = Query(default=1, ge=1, description="Page number"),
    limit: int = Query(default=30, ge=1, le=100, description="Items per page"),
) -> list:
    """List issues in a repository."""
    return await _gitea_request(
        "GET",
        f"{settings.gitea_api_url}/repos/{owner}/{repo}/issues",
        params={"state": state, "page": page, "limit": limit},
    )


@router.post("/pulls", summary="Create a pull request", operation_id="GiteaCreatePR")
async def create_pr(pr: PRCreate) -> dict:
    """
    Create a new pull request.

    Creates a PR from the head branch to the base branch.
    """
    logger.info(
        "Creating PR",
        owner=pr.owner,
        repo=pr.repo,
        head=pr.head,
        base=pr.base,
        title=pr.title,
    )

    result = await _gitea_request(
        "POST",
        f"{settings.gitea_api_url}/repos/{pr.owner}/{pr.repo}/pulls",
        json={
            "title": pr.title,
            "body": pr.body,
            "head": pr.head,
            "base": pr.base,
            "draft": pr.draft,
        },
    )

    logger.info(
        "PR created",
        owner=pr.owner,
        repo=pr.repo,
        index=result.get("number"),
        url=result.get("html_url"),
    )
    return result


@router.get("/pulls/{owner}/{repo}", summary="List pull requests", operation_id="GiteaListPRs")
async def list_prs(
    owner: str,
    repo: str,
    state: str = Query(default="open", description="PR state: open, closed, or all"),
    page: int = Query(default=1, ge=1, description="Page number"),
    limit: int = Query(default=30, ge=1, le=100, description="Items per page"),
) -> list:
    """List pull requests in a repository."""
    return await _gitea_request(
        "GET",
        f"{settings.gitea_api_url}/repos/{owner}/{repo}/pulls",
        params={"state": state, "page": page, "limit": limit},
    )


@router.post(
    "/issues/{owner}/{repo}/{index}/comments",
    summary="Post a comment on an issue or PR",
    operation_id="GiteaPostComment",
)
async def post_comment(owner: str, repo: str, index: int, comment: CommentCreate) -> dict:
    """
    Post a comment on an issue or pull request.

    Works for both issues and PRs — they share the same comments endpoint in Gitea.
    """
    logger.info("Posting comment", owner=owner, repo=repo, index=index)

    result = await _gitea_request(
        "POST",
        f"{settings.gitea_api_url}/repos/{owner}/{repo}/issues/{index}/comments",
        json={"body": comment.body},
    )

    logger.info("Comment posted", owner=owner, repo=repo, index=index)
    return result


@router.get(
    "/issues/{owner}/{repo}/{index}/comments",
    summary="List comments on an issue or PR",
    operation_id="GiteaListComments",
)
async def list_comments(
    owner: str,
    repo: str,
    index: int,
    page: int = Query(default=1, ge=1, description="Page number"),
    limit: int = Query(default=30, ge=1, le=100, description="Items per page"),
) -> list:
    """List comments on an issue or pull request."""
    return await _gitea_request(
        "GET",
        f"{settings.gitea_api_url}/repos/{owner}/{repo}/issues/{index}/comments",
        params={"page": page, "limit": limit},
    )


@router.get("/health", summary="Check Gitea connection", operation_id="GiteaHealth")
async def gitea_health() -> dict:
    """Check if Gitea is configured and accessible."""
    if not settings.is_gitea_configured():
        return {"status": "error", "message": "Gitea is not configured"}

    try:
        response = await _gitea_request("GET", f"{settings.gitea_api_url}/user/info")
        return {
            "status": "ok",
            "username": response.get("login", "unknown"),
            "instance": settings.GITEA_INSTANCE_URL,
        }
    except GiteaError as e:
        return {"status": "error", "message": str(e)}
