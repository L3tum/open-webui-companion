"""Gitea integration endpoints for Open-WebUI.

Provides tools for repository management, file operations, issue tracking,
pull requests, and commenting via the Gitea REST API.
"""

import base64

import httpx
from fastapi import APIRouter, Query
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


class FileListResponse(BaseModel):
    """Schema for a single file/directory entry in a listing."""
    name: str = Field(..., description="Entry name")
    path: str = Field(..., description="Full path within the repo")
    type: str = Field(..., description="Entry type: file or dir")
    size: int = Field(default=0, description="Size in bytes (files only)")
    download_url: str = Field(default="", description="Direct download URL (files only)")


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

    Creates a new file (POST) or updates an existing one (PUT with SHA).
    """
    logger.info(
        "Creating/updating file",
        owner=file.owner,
        repo=file.repo,
        path=file.path,
        branch=file.branch or "default",
    )

    # Determine branch — fetch repo default if not provided
    branch = file.branch
    if not branch:
        try:
            repo_info = await _gitea_request(
                "GET",
                f"{settings.gitea_api_url}/repos/{file.owner}/{file.repo}",
            )
            branch = repo_info.get("default_branch", "main")
            logger.info(
                "Using repo default branch",
                owner=file.owner,
                repo=file.repo,
                branch=branch,
            )
        except GiteaError:
            branch = "main"  # fallback

    # Check if file exists
    file_exists = False
    file_sha = None
    try:
        existing = await _gitea_request(
            "GET",
            f"{settings.gitea_api_url}/repos/{file.owner}/{file.repo}/contents/{file.path}",
            params={"ref": branch},
        )
        file_exists = True
        file_sha = existing["sha"]
    except GiteaError as e:
        if e.status_code != 404:
            raise

    encoded_content = base64.b64encode(file.content.encode()).decode()
    payload = {
        "content": encoded_content,
        "message": file.message,
        "branch": branch,
    }

    if file_exists:
        # Update existing file — PUT with SHA
        payload["sha"] = file_sha
        method = "PUT"
        logger.info(
            "Updating existing file",
            owner=file.owner,
            repo=file.repo,
            path=file.path,
            sha=file_sha[:12],
        )
    else:
        # Create new file — POST without SHA
        method = "POST"
        logger.info(
            "Creating new file",
            owner=file.owner,
            repo=file.repo,
            path=file.path,
        )

    result = await _gitea_request(
        method,
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


@router.get("/files/{owner}/{repo}/{path:path}/ls", summary="List files in directory", operation_id="GiteaListFiles")
async def list_files(
    owner: str,
    repo: str,
    path: str = "",
    branch: str = Query(default="", description="Branch name"),
    filter: str = Query(default="", description="Glob pattern to filter by (e.g. '*.py', 'README*')"),
) -> list[FileListResponse]:
    """
    List files and directories at the given path.

    Supports an optional glob filter to narrow results.
    """
    params = {"ref": branch} if branch else {}

    entries = await _gitea_request(
        "GET",
        f"{settings.gitea_api_url}/repos/{owner}/{repo}/contents/{path}",
        params=params,
    )

    # Apply filter if provided
    if filter:
        import fnmatch
        entries = [e for e in entries if fnmatch.fnmatch(e.get("name", ""), filter)]

    return [
        FileListResponse(
            name=e.get("name", ""),
            path=e.get("path", ""),
            type=e.get("type", "file"),
            size=e.get("size", 0),
            download_url=e.get("download_url", ""),
        )
        for e in entries
    ]


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


# ─── Pipeline / Actions endpoints ───────────────────────────────────────────


class PipelineStatusResponse(BaseModel):
    """Schema for pipeline status response."""

    run_id: int = Field(..., description="Workflow run ID")
    status: str = Field(..., description="Run status: success, failure, running, waiting, cancelled, skipped")
    conclusion: str = Field(default="", description="Run conclusion: success, failure, neutral, cancelled, timed_out, or empty if still running")
    name: str = Field(default="", description="Workflow name")
    display_title: str = Field(default="", description="Human-readable title for the run")
    event: str = Field(default="", description="Trigger event: push, pull_request, schedule, etc.")
    branch: str = Field(default="", description="Branch the run was triggered on")
    commit_sha: str = Field(default="", description="Commit SHA")
    html_url: str = Field(default="", description="URL to view the run in the web UI")
    started_at: str = Field(default="", description="ISO 8601 timestamp when the run started")
    updated_at: str = Field(default="", description="ISO 8601 timestamp of last update")


class PipelineJob(BaseModel):
    """Schema for a single job in a pipeline run."""

    job_id: int = Field(..., description="Job ID")
    name: str = Field(default="", description="Job name")
    status: str = Field(default="", description="Job status")
    conclusion: str = Field(default="", description="Job conclusion")
    started_at: str = Field(default="", description="ISO 8601 timestamp when the job started")


class PipelineOutputResponse(BaseModel):
    """Schema for pipeline output/logs response."""

    run_id: int = Field(..., description="Workflow run ID")
    status: str = Field(..., description="Run status")
    conclusion: str = Field(default="", description="Run conclusion")
    name: str = Field(default="", description="Workflow name")
    jobs: list[PipelineJob] = Field(default_factory=list, description="List of jobs in this run")
    logs: str = Field(default="", description="Combined log output from all jobs")


@router.get(
    "/pulls/{owner}/{repo}/{index}/pipeline",
    summary="Get pipeline status for a PR",
    operation_id="GiteaGetPRPipeline",
)
async def get_pr_pipeline(
    owner: str,
    repo: str,
    index: int,
) -> PipelineStatusResponse:
    """
    Get the latest pipeline (workflow run) status for a pull request.

    Fetches the most recent workflow run triggered by this PR and returns
    its status. An agent can use this after getting PR details to check
    whether CI has passed.

    Returns the latest run's status, which can be:
    - `success`: Pipeline passed
    - `failure`: Pipeline failed
    - `running`: Pipeline is still in progress
    - `waiting`: Pipeline is waiting for a runner
    - `cancelled`: Pipeline was cancelled
    - `skipped`: Pipeline was skipped
    """
    logger.info("Getting PR pipeline status", owner=owner, repo=repo, pr_index=index)

    # Get PR details to find the head branch
    pr_data = await _gitea_request(
        "GET",
        f"{settings.gitea_api_url}/repos/{owner}/{repo}/pulls/{index}",
    )

    head_branch = pr_data.get("head", {}).get("ref", "")
    if not head_branch:
        raise GiteaError(f"Could not determine head branch for PR #{index}", status_code=404)

    logger.info("Looking up pipeline for branch", branch=head_branch)

    # List workflow runs, filtered by the PR's head branch and pull_request event
    runs_data = await _gitea_request(
        "GET",
        f"{settings.gitea_api_url}/repos/{owner}/{repo}/actions/runs",
        params={
            "branch": head_branch,
            "event": "pull_request",
            "per_page": 1,
        },
    )

    # The API returns {"workflow_runs": [...], "total_count": N}
    runs = runs_data if isinstance(runs_data, list) else runs_data.get("workflow_runs", [])

    if not runs:
        return PipelineStatusResponse(
            run_id=0,
            status="pending",
            conclusion="",
            name="",
            display_title=f"No pipeline runs found for PR #{index}",
            event="",
            branch=head_branch,
            commit_sha="",
            html_url="",
            started_at="",
            updated_at="",
        )

    run = runs[0]

    return PipelineStatusResponse(
        run_id=run.get("id", 0),
        status=run.get("status", "unknown"),
        conclusion=run.get("conclusion", ""),
        name=run.get("name", ""),
        display_title=run.get("display_title", ""),
        event=run.get("event", ""),
        branch=run.get("head_branch", head_branch),
        commit_sha=run.get("head_sha", ""),
        html_url=run.get("html_url", ""),
        started_at=run.get("started_at", ""),
        updated_at=run.get("updated_at", ""),
    )


@router.get(
    "/actions/runs/{owner}/{repo}/{run_id}/logs",
    summary="Get pipeline output/logs",
    operation_id="GiteaGetPipelineOutput",
)
async def get_pipeline_output(
    owner: str,
    repo: str,
    run_id: int,
) -> PipelineOutputResponse:
    """
    Get the full pipeline output and logs for a workflow run.

    Fetches all jobs in the specified workflow run and retrieves their logs.
    This is useful for debugging pipeline failures — after checking the status
    with GiteaGetPRPipeline, you can fetch the detailed output.

    Returns combined logs from all jobs, along with job-level status information.
    """
    logger.info("Getting pipeline output", owner=owner, repo=repo, run_id=run_id)

    # Get run details
    run_data = await _gitea_request(
        "GET",
        f"{settings.gitea_api_url}/repos/{owner}/{repo}/actions/runs/{run_id}",
    )

    # List jobs for this run
    jobs_data = await _gitea_request(
        "GET",
        f"{settings.gitea_api_url}/repos/{owner}/{repo}/actions/runs/{run_id}/jobs",
    )

    jobs_list = jobs_data if isinstance(jobs_data, list) else jobs_data.get("workflow_jobs", [])

    # Build job summaries
    jobs = [
        PipelineJob(
            job_id=job.get("id", 0),
            name=job.get("name", ""),
            status=job.get("status", ""),
            conclusion=job.get("conclusion", ""),
            started_at=job.get("started_at", ""),
        )
        for job in jobs_list
    ]

    # Fetch logs for each job
    log_sections = []
    for job in jobs_list:
        job_id = job.get("id", 0)
        job_name = job.get("name", f"job-{job_id}")
        log_sections.append(f"\n{'='*60}")
        log_sections.append(f"Job: {job_name} (ID: {job_id})")
        log_sections.append(f"Status: {job.get('status', 'unknown')}")
        log_sections.append(f"Conclusion: {job.get('conclusion', 'N/A')}")
        log_sections.append(f"{'='*60}\n")

        try:
            # Fetch job logs
            log_response = await _gitea_request(
                "GET",
                f"{settings.gitea_api_url}/repos/{owner}/{repo}/actions/runs/{run_id}/jobs/{job_id}/logs",
            )
            # The logs endpoint may return text directly or a URL
            if isinstance(log_response, str):
                log_sections.append(log_response)
            elif isinstance(log_response, dict):
                log_url = log_response.get("url", "")
                if log_url:
                    log_sections.append(f"Logs URL: {log_url}")
                    # Try to fetch the actual log content
                    try:
                        settings.require_gitea_config()
                        async with httpx.AsyncClient(timeout=30.0) as client:
                            log_content = await client.get(
                                log_url,
                                headers=settings.gitea_headers,
                            )
                            log_content.raise_for_status()
                            log_sections.append(log_content.text[:50000])  # Limit log size
                    except Exception as e:
                        log_sections.append(f"[Could not fetch logs: {e}]")
                else:
                    log_sections.append(str(log_response))
            else:
                log_sections.append(str(log_response))
        except GiteaError as e:
            log_sections.append(f"[Failed to fetch logs for job {job_id}: {e}]")
            logger.warning(
                "Failed to fetch job logs",
                owner=owner,
                repo=repo,
                run_id=run_id,
                job_id=job_id,
                error=str(e),
            )

    combined_logs = "\n".join(log_sections)

    return PipelineOutputResponse(
        run_id=run_data.get("id", run_id),
        status=run_data.get("status", "unknown"),
        conclusion=run_data.get("conclusion", ""),
        name=run_data.get("name", ""),
        jobs=jobs,
        logs=combined_logs,
    )
