from __future__ import annotations

import time
from pathlib import Path

import pytest

from whatsapp.bridge import WhatsAppBridge
from whatsapp.cloud_api import CloudAPIClient
from whatsapp.config import Settings
from whatsapp.store import ConversationStore

APP_SECRET = "test-app-secret"
VERIFY_TOKEN = "test-verify-token"
PHONE_NUMBER_ID = "1234567890"
GRAPH_BASE = "https://graph.test"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        access_token="test-token",
        phone_number_id=PHONE_NUMBER_ID,
        app_secret=APP_SECRET,
        verify_token=VERIFY_TOKEN,
        api_version="v23.0",
        graph_base=GRAPH_BASE,
        db_path=tmp_path / "test.db",
        media_dir=tmp_path / "media",
    )


@pytest.fixture
def store(settings: Settings) -> ConversationStore:
    return ConversationStore(settings.db_path)


@pytest.fixture
def bridge(settings: Settings, store: ConversationStore) -> WhatsAppBridge:
    return WhatsAppBridge(settings, store=store, client=CloudAPIClient(settings))


def inbound_payload(
    *,
    message_id: str = "wamid.INBOUND1",
    wa_id: str = "15550001111",
    text: str = "Hello there",
    name: str = "Ada Lovelace",
    timestamp: int | None = None,
) -> dict:
    """A realistic single-text-message webhook body."""
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA_ID",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "15559998888",
                                "phone_number_id": PHONE_NUMBER_ID,
                            },
                            "contacts": [
                                {"profile": {"name": name}, "wa_id": wa_id}
                            ],
                            "messages": [
                                {
                                    "from": wa_id,
                                    "id": message_id,
                                    "timestamp": str(
                                        timestamp
                                        if timestamp is not None
                                        else int(time.time())
                                    ),
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }
