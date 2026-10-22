"""
Configuration management for the Open-WebUI Companion Server.

Loads settings from environment variables and an optional .env file.
"""

import os
from pathlib import Path

from dotenv import load_dotenv


def _env_int(name: str, default: int) -> int:
    """Parse an integer environment variable, falling back to ``default`` when unset or malformed."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    """Parse a float environment variable, falling back to ``default`` when unset or malformed."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


# Load .env file from project root if it exists
_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
if _ENV_FILE.exists():
    load_dotenv(_ENV_FILE)


class Settings:
    """Application settings loaded from environment variables."""

    # Gitea Configuration
    GITEA_INSTANCE_URL: str = os.getenv("GITEA_INSTANCE_URL", "https://gitea.com")
    GITEA_TOKEN: str = os.getenv("GITEA_TOKEN", "")
    GITEA_USERNAME: str = os.getenv("GITEA_USERNAME", "")

    # Open-WebUI Configuration
    OWUI_INSTANCE_URL: str = os.getenv("OWUI_INSTANCE_URL", "")
    OWUI_TOKEN: str = os.getenv("OWUI_TOKEN", "")
    # Accept per-user tokens forwarded from Open-WebUI. When enabled,
    # chat-data endpoints REQUIRE a forwarded user token and never use
    # the static OWUI_TOKEN; when disabled everything runs as OWUI_TOKEN
    # (legacy mode).
    OWUI_ALLOW_USER_TOKEN: bool = os.getenv("OWUI_ALLOW_USER_TOKEN", "true").lower() == "true"
    # Shared secret (must equal Open-WebUI's JWT_SECRET) for verifying the
    # signed X-OpenWebUI-User-Jwt header. When set, forwarded-token
    # requests must carry a valid header (fail-closed). Empty = off.
    OWUI_FORWARD_JWT_SECRET: str = os.getenv("OWUI_FORWARD_JWT_SECRET", "")
    # Seconds to cache validated user tokens
    OWUI_TOKEN_CACHE_TTL: int = _env_int("OWUI_TOKEN_CACHE_TTL", 300)

    # Company Vetting Configuration
    # The single optional search-engine query (exact company name) for the
    # third-party-mention check. Everything else is a direct lookup
    # (RDAP, the website itself, the Wayback Machine).
    VET_ENABLE_MENTIONS: bool = os.getenv("VET_ENABLE_MENTIONS", "true").lower() == "true"
    VET_MENTION_MAX_RESULTS: int = _env_int("VET_MENTION_MAX_RESULTS", 10)
    VET_HTTP_TIMEOUT: float = _env_float("VET_HTTP_TIMEOUT", 20.0)
    # A domain younger than this is flagged; combined with a foreign
    # registrar (or zero mentions + no registry number) it rejects.
    VET_MIN_DOMAIN_AGE_DAYS: int = _env_int("VET_MIN_DOMAIN_AGE_DAYS", 90)
    # A foreign registrar is only a rejection together with a domain this
    # young or younger; older foreign-registrar domains are flagged only.
    VET_FOREIGN_REGISTRAR_MAX_AGE_DAYS: int = _env_int(
        "VET_FOREIGN_REGISTRAR_MAX_AGE_DAYS", 180
    )
    # Seconds to reuse an identical verdict (same name + domain).
    VET_CACHE_TTL: int = _env_int("VET_CACHE_TTL", 86400)
    # Optional direct registry lookup, e.g.
    # "https://api.myregistry.local/search?name={query}" ({query} = the
    # URL-encoded company name). Empty = registry check derived from the
    # Impressum only (handelsregister.de is a stateful JS app, not
    # plain-GET queryable).
    VET_REGISTRY_URL_TEMPLATE: str = os.getenv("VET_REGISTRY_URL_TEMPLATE", "")
    VET_USER_AGENT: str = os.getenv("VET_USER_AGENT", "open-webui-companion-vetting/1.0")

    # Server Configuration
    HOST: str = os.getenv("HOST", "0.0.0.0")
    PORT: int = _env_int("PORT", 8090)
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "info")


settings = Settings()
