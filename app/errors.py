"""Custom exceptions and error handling middleware.

Provides consistent error responses and structured error logging.
"""

import traceback
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse

from app.logging import get_logger

logger = get_logger(__name__)


class GiteaError(Exception):
    """Raised when a Gitea API call fails."""

    def __init__(self, message: str, status_code: int = 502, response_data: Any = None):
        self.status_code = status_code
        self.response_data = response_data
        super().__init__(message)


class OWUIError(Exception):
    """Raised when an Open-WebUI API call fails."""

    def __init__(self, message: str, status_code: int = 502, response_data: Any = None):
        self.status_code = status_code
        self.response_data = response_data
        super().__init__(message)


class ConfigError(Exception):
    """Raised when required configuration is missing."""

    pass


def _error_response(detail: str, status_code: int) -> JSONResponse:
    """Create a consistent JSON error response."""
    return JSONResponse(
        status_code=status_code,
        content={
            "error": True,
            "detail": detail,
            "status_code": status_code,
        },
    )


def add_error_handlers(app: FastAPI) -> None:
    """Register global exception handlers with the FastAPI app."""

    @app.exception_handler(HTTPException)
    async def handle_http_exception(request: Request, exc: HTTPException) -> JSONResponse:
        """Handle FastAPI HTTPExceptions with consistent formatting."""
        logger.warning(
            "HTTP error",
            status_code=exc.status_code,
            detail=exc.detail,
            path=request.url.path,
            method=request.method,
        )
        return _error_response(exc.detail, exc.status_code)

    @app.exception_handler(GiteaError)
    async def handle_gitea_error(request: Request, exc: GiteaError) -> JSONResponse:
        """Handle Gitea API errors."""
        logger.error(
            "Gitea API error",
            status_code=exc.status_code,
            detail=str(exc),
            path=request.url.path,
        )
        return _error_response(str(exc), exc.status_code)

    @app.exception_handler(OWUIError)
    async def handle_owui_error(request: Request, exc: OWUIError) -> JSONResponse:
        """Handle Open-WebUI API errors."""
        logger.error(
            "Open-WebUI API error",
            status_code=exc.status_code,
            detail=str(exc),
            path=request.url.path,
        )
        return _error_response(str(exc), exc.status_code)

    @app.exception_handler(ConfigError)
    async def handle_config_error(request: Request, exc: ConfigError) -> JSONResponse:
        """Handle configuration errors."""
        logger.error("Configuration error: %s", exc)
        return _error_response(str(exc), status.HTTP_500_INTERNAL_SERVER_ERROR)

    @app.exception_handler(httpx.HTTPStatusError)
    async def handle_httpx_error(request: Request, exc: httpx.HTTPStatusError) -> JSONResponse:
        """Handle HTTP client errors from external APIs."""
        logger.error(
            "External API error",
            url=str(exc.request.url),
            status_code=exc.response.status_code,
            response=exc.response.text[:500],
        )
        return _error_response(
            f"External API error: {exc.response.status_code}",
            status.HTTP_502_BAD_GATEWAY,
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        """Catch-all for unexpected errors."""
        logger.exception(
            "Unexpected error",
            path=request.url.path,
            method=request.method,
            exc_type=type(exc).__name__,
        )
        return _error_response(
            "An unexpected error occurred. Check server logs for details.",
            status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
