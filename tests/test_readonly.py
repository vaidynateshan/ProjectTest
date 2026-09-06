"""Read-only mode: receiving messages without send credentials.

Message content arrives inside the webhook payload, so an access token and
phone number ID are needed only for sending and media download.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from whatsapp import mcp_server
from whatsapp.bridge import WhatsAppBridge
from whatsapp.cloud_api import CloudAPIClient
from whatsapp.config import ConfigError, Settings, load_settings
from whatsapp.signature import SIGNATURE_HEADER, compute_signature
from whatsapp.store import ConversationStore
from whatsapp.webhook import create_app

from .conftest import APP_SECRET, VERIFY_TOKEN, inbound_payload


@pytest.fixture
def readonly_settings(tmp_path) -> Settings:
    return Settings(
        app_secret=APP_SECRET,
        verify_token=VERIFY_TOKEN,
        db_path=tmp_path / "ro.db",
        media_dir=tmp_path / "media",
    )


@pytest.fixture
def readonly_bridge(readonly_settings: Settings) -> WhatsAppBridge:
    return WhatsAppBridge(
        readonly_settings,
        store=ConversationStore(readonly_settings.db_path),
        client=CloudAPIClient(readonly_settings),
    )


def test_two_variables_are_enough_to_start() -> None:
    settings = load_settings(
        {"WHATSAPP_APP_SECRET": "a" * 32, "WHATSAPP_VERIFY_TOKEN": "tok"}
    )
    assert settings.can_send is False


def test_send_credentials_enable_sending() -> None:
    settings = load_settings(
        {
            "WHATSAPP_APP_SECRET": "a" * 32,
            "WHATSAPP_VERIFY_TOKEN": "tok",
            "WHATSAPP_ACCESS_TOKEN": "EAA...",
            "WHATSAPP_PHONE_NUMBER_ID": "123",
        }
    )
    assert settings.can_send is True


def test_app_secret_and_verify_token_are_still_required() -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_settings({"WHATSAPP_ACCESS_TOKEN": "EAA..."})
    message = str(excinfo.value)
    assert "WHATSAPP_APP_SECRET" in message
    assert "WHATSAPP_VERIFY_TOKEN" in message
    # The send-only variables must not be demanded.
    assert "WHATSAPP_PHONE_NUMBER_ID" not in message


def test_webhook_records_messages_without_send_credentials(
    readonly_bridge: WhatsAppBridge,
) -> None:
    """The whole point: reading works with no access token at all."""
    with TestClient(create_app(bridge=readonly_bridge)) as client:
        body = json.dumps(inbound_payload(text="Do you deliver on Sundays?")).encode()
        response = client.post(
            "/webhook",
            content=body,
            headers={
                SIGNATURE_HEADER: compute_signature(APP_SECRET, body),
                "Content-Type": "application/json",
            },
        )

    assert response.status_code == 200
    assert response.json()["messages_stored"] == 1

    messages = readonly_bridge.read_thread("15550001111")
    assert messages[0]["text"] == "Do you deliver on Sundays?"
    assert readonly_bridge.thread("15550001111").profile_name == "Ada Lovelace"


def test_verification_handshake_works_without_send_credentials(
    readonly_bridge: WhatsAppBridge,
) -> None:
    with TestClient(create_app(bridge=readonly_bridge)) as client:
        response = client.get(
            "/webhook",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": VERIFY_TOKEN,
                "hub.challenge": "42",
            },
        )
    assert response.status_code == 200
    assert response.text == "42"


def test_signature_check_still_rejects_forgeries_in_readonly(
    readonly_bridge: WhatsAppBridge,
) -> None:
    with TestClient(create_app(bridge=readonly_bridge)) as client:
        body = json.dumps(inbound_payload()).encode()
        response = client.post(
            "/webhook",
            content=body,
            headers={
                SIGNATURE_HEADER: compute_signature("attacker", body),
                "Content-Type": "application/json",
            },
        )
    assert response.status_code == 403
    assert readonly_bridge.list_threads() == []


class TestReadOnlyMcpTools:
    @pytest.fixture(autouse=True)
    def _bridge(self, monkeypatch, readonly_bridge: WhatsAppBridge) -> None:
        monkeypatch.setattr(mcp_server, "_bridge", readonly_bridge)

    def test_read_tools_work(self, readonly_bridge: WhatsAppBridge) -> None:
        from whatsapp.models import parse_webhook

        readonly_bridge.ingest(parse_webhook(inbound_payload(text="hello there")))

        assert "15550001111" in mcp_server.list_threads()
        assert "hello there" in mcp_server.read_thread("15550001111")
        assert "hello there" in mcp_server.search_messages("hello")

    async def test_send_tools_explain_read_only_mode(self) -> None:
        for result in [
            await mcp_server.send_message("15550001111", "hi"),
            await mcp_server.send_template("15550001111", "greeting"),
            await mcp_server.send_media("15550001111", "image", link="https://x/i.jpg"),
            await mcp_server.mark_read("wamid.X"),
        ]:
            assert "read-only mode" in result

    async def test_media_download_names_the_missing_token(self) -> None:
        result = await mcp_server.download_media("media-1")
        assert "access token" in result
