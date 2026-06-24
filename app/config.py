"""Configuration management for the companion server."""

import os
from dotenv import load_dotenv

from app.errors import ConfigError

# Load environment variables from .env file
load_dotenv()


class Settings:
    """Application settings loaded from environment variables."""

    # Gitea Configuration
    GITEA_INSTANCE_URL: str = os.getenv("GITEA_INSTANCE_URL", "https://gitea.com")
    GITEA_TOKEN: str = os.getenv("GITEA_TOKEN", "")
    EXPOSE_GITEA_TOKEN: bool = (
        os.getenv("EXPOSE_GITEA_TOKEN", "false").lower() == "true"
    )

    # Open-WebUI Configuration
    OWUI_INSTANCE_URL: str = os.getenv("OWUI_INSTANCE_URL", "http://localhost:8080")
    OWUI_TOKEN: str = os.getenv("OWUI_TOKEN", "")

    # Notes Sync Configuration
    NOTES_SYNC_KB_ID: str = os.getenv("NOTES_SYNC_KB_ID", "")
    NOTES_SYNC_INTERVAL: int = int(os.getenv("NOTES_SYNC_INTERVAL", "300"))

    # Server Configuration
    HOST: str = os.getenv("HOST", "0.0.0.0")
    PORT: int = int(os.getenv("PORT", "8090"))
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "info")
    LOG_STRUCTURED: bool = os.getenv("LOG_STRUCTURED", "true").lower() == "true"

    # Evidence Configuration
    EVIDENCE_DIR: str = os.getenv("EVIDENCE_DIR", "/data")
    EVIDENCE_FILE: str = os.path.join(EVIDENCE_DIR, "evidence.json")

    @property
    def gitea_api_url(self) -> str:
        """Get the Gitea API base URL."""
        url = self.GITEA_INSTANCE_URL.rstrip("/")
        return f"{url}/api/v1"

    @property
    def gitea_headers(self) -> dict:
        """Get headers for Gitea API requests."""
        return {
            "Authorization": f"token {self.GITEA_TOKEN}",
            "Content-Type": "application/json",
        }

    @property
    def owui_api_url(self) -> str:
        """Get the Open-WebUI API base URL."""
        url = self.OWUI_INSTANCE_URL.rstrip("/")
        return f"{url}/api/v1"

    @property
    def owui_headers(self) -> dict:
        """Get headers for Open-WebUI API requests."""
        return {
            "Authorization": f"Bearer {self.OWUI_TOKEN}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def is_gitea_configured(self) -> bool:
        """Check if Gitea is properly configured."""
        return bool(self.GITEA_TOKEN)

    def require_gitea_config(self) -> None:
        """Raise ConfigError if Gitea is not configured."""
        if not self.is_gitea_configured():
            raise ConfigError(
                "Gitea is not configured. Set GITEA_TOKEN and GITEA_INSTANCE_URL environment variables."
            )

    def is_owui_configured(self) -> bool:
        """Check if Open-WebUI is properly configured."""
        return bool(self.OWUI_TOKEN)

    def require_owui_config(self) -> None:
        """Raise ConfigError if Open-WebUI is not configured."""
        if not self.is_owui_configured():
            raise ConfigError(
                "Open-WebUI is not configured. Set OWUI_TOKEN and OWUI_INSTANCE_URL environment variables."
            )


# Global settings instance
settings = Settings()
