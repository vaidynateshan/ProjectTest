"""Configuration loaded from the environment.

Every value comes from an environment variable so that no credential is ever
committed. See ``.env.example`` for where each one is found in Meta's
App Dashboard.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

#: Graph API version pinned by default. Meta keeps a version usable for ~2
#: years; bump it deliberately rather than tracking whatever is newest.
DEFAULT_API_VERSION = "v23.0"

DEFAULT_GRAPH_BASE = "https://graph.facebook.com"


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or malformed."""


@dataclass(frozen=True)
class Settings:
    """Everything the bridge needs to talk to the Cloud API."""

    access_token: str
    phone_number_id: str
    app_secret: str
    verify_token: str
    api_version: str = DEFAULT_API_VERSION
    graph_base: str = DEFAULT_GRAPH_BASE
    db_path: Path = Path("whatsapp.db")
    media_dir: Path = Path("media")

    @property
    def base_url(self) -> str:
        return f"{self.graph_base.rstrip('/')}/{self.api_version}"

    @property
    def messages_url(self) -> str:
        return f"{self.base_url}/{self.phone_number_id}/messages"

    @property
    def media_upload_url(self) -> str:
        return f"{self.base_url}/{self.phone_number_id}/media"

    def media_url(self, media_id: str) -> str:
        return f"{self.base_url}/{media_id}"


_REQUIRED = {
    "WHATSAPP_ACCESS_TOKEN": "access_token",
    "WHATSAPP_PHONE_NUMBER_ID": "phone_number_id",
    "WHATSAPP_APP_SECRET": "app_secret",
    "WHATSAPP_VERIFY_TOKEN": "verify_token",
}


def load_settings(env: dict[str, str] | None = None) -> Settings:
    """Build :class:`Settings` from the environment.

    Reports *all* missing variables at once rather than failing on the first,
    which matters because these are set up in four different places in Meta's
    dashboard.
    """
    source = os.environ if env is None else env

    missing = [name for name in _REQUIRED if not source.get(name)]
    if missing:
        raise ConfigError(
            "Missing required environment variable(s): "
            + ", ".join(sorted(missing))
            + ". Copy .env.example to .env and fill it in."
        )

    return Settings(
        access_token=source["WHATSAPP_ACCESS_TOKEN"],
        phone_number_id=source["WHATSAPP_PHONE_NUMBER_ID"],
        app_secret=source["WHATSAPP_APP_SECRET"],
        verify_token=source["WHATSAPP_VERIFY_TOKEN"],
        api_version=source.get("WHATSAPP_API_VERSION") or DEFAULT_API_VERSION,
        graph_base=source.get("WHATSAPP_GRAPH_BASE") or DEFAULT_GRAPH_BASE,
        db_path=Path(source.get("WHATSAPP_DB_PATH") or "whatsapp.db"),
        media_dir=Path(source.get("WHATSAPP_MEDIA_DIR") or "media"),
    )
