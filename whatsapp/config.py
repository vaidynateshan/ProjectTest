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

    app_secret: str
    verify_token: str
    #: Only needed to send messages and to download media attachments.
    #: Reading works without them: message content arrives inside the webhook.
    access_token: str = ""
    phone_number_id: str = ""
    #: Only needed to serve the Coexistence onboarding page at /onboard.
    app_id: str = ""
    config_id: str = ""
    api_version: str = DEFAULT_API_VERSION
    graph_base: str = DEFAULT_GRAPH_BASE
    db_path: Path = Path("whatsapp.db")
    media_dir: Path = Path("media")

    @property
    def can_onboard(self) -> bool:
        """Whether the Embedded Signup page at /onboard can be served."""
        return bool(self.app_id and self.config_id)

    @property
    def can_send(self) -> bool:
        """Whether outbound calls are configured.

        False puts the bridge in read-only mode: the webhook still records
        everything, but nothing can be sent and media cannot be fetched.
        """
        return bool(self.access_token and self.phone_number_id)

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


#: Receiving messages needs only these two: the app secret verifies the
#: signature on incoming webhooks, and the verify token completes the
#: subscription handshake. Message content arrives in the payload itself.
_REQUIRED = {
    "WHATSAPP_APP_SECRET": "app_secret",
    "WHATSAPP_VERIFY_TOKEN": "verify_token",
}

#: Needed only to send messages and download media.
_SEND_ONLY = ("WHATSAPP_ACCESS_TOKEN", "WHATSAPP_PHONE_NUMBER_ID")


def load_settings(env: dict[str, str] | None = None) -> Settings:
    """Build :class:`Settings` from the environment.

    Only the app secret and verify token are required. Without an access
    token and phone number ID the bridge runs read-only: the webhook records
    every inbound message, but sending and media download are unavailable.
    Reports all missing variables at once rather than failing on the first.
    """
    source = os.environ if env is None else env

    missing = [name for name in _REQUIRED if not source.get(name)]
    if missing:
        raise ConfigError(
            "Missing required environment variable(s): "
            + ", ".join(sorted(missing))
            + ". Copy .env.example to .env and fill it in."
            + " (Only these two are needed to receive messages.)"
        )

    return Settings(
        app_secret=source["WHATSAPP_APP_SECRET"],
        verify_token=source["WHATSAPP_VERIFY_TOKEN"],
        access_token=source.get("WHATSAPP_ACCESS_TOKEN") or "",
        phone_number_id=source.get("WHATSAPP_PHONE_NUMBER_ID") or "",
        app_id=source.get("META_APP_ID") or "",
        config_id=source.get("META_CONFIG_ID") or "",
        api_version=source.get("WHATSAPP_API_VERSION") or DEFAULT_API_VERSION,
        graph_base=source.get("WHATSAPP_GRAPH_BASE") or DEFAULT_GRAPH_BASE,
        db_path=Path(source.get("WHATSAPP_DB_PATH") or "whatsapp.db"),
        media_dir=Path(source.get("WHATSAPP_MEDIA_DIR") or "media"),
    )
