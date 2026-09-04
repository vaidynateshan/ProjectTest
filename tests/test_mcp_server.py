from __future__ import annotations

import time

import httpx
import pytest
import respx

from whatsapp import mcp_server
from whatsapp.bridge import WhatsAppBridge
from whatsapp.config import Settings
from whatsapp.models import parse_webhook
from whatsapp.store import CUSTOMER_SERVICE_WINDOW_SECONDS

from .conftest import inbound_payload

SEND_RESPONSE = {
    "messaging_product": "whatsapp",
    "contacts": [{"input": "15550001111", "wa_id": "15550001111"}],
    "messages": [{"id": "wamid.SENT"}],
}


@pytest.fixture(autouse=True)
def use_test_bridge(monkeypatch: pytest.MonkeyPatch, bridge: WhatsAppBridge) -> None:
    """Point the module-level bridge at the temp store instead of the env."""
    monkeypatch.setattr(mcp_server, "_bridge", bridge)


async def test_all_tools_are_exposed() -> None:
    names = {tool.name for tool in await mcp_server.mcp.list_tools()}
    assert names == {
        "list_threads",
        "read_thread",
        "search_messages",
        "send_message",
        "send_template",
        "send_media",
        "download_media",
        "mark_read",
    }


def test_list_threads_explains_an_empty_store() -> None:
    assert "No conversations stored yet" in mcp_server.list_threads()


def test_list_threads_shows_the_window_state(bridge: WhatsAppBridge) -> None:
    bridge.ingest(parse_webhook(inbound_payload(text="hello")))

    output = mcp_server.list_threads()
    assert "15550001111" in output
    assert "Ada Lovelace" in output
    assert "window open until" in output


def test_read_thread_warns_when_the_window_has_closed(
    bridge: WhatsAppBridge,
) -> None:
    stale = int(time.time()) - CUSTOMER_SERVICE_WINDOW_SECONDS - 60
    bridge.ingest(parse_webhook(inbound_payload(text="old question", timestamp=stale)))

    output = mcp_server.read_thread("15550001111")
    assert "CLOSED" in output
    assert "send_template" in output
    assert "old question" in output


def test_read_thread_rejects_a_nonsense_number() -> None:
    assert mcp_server.read_thread("not-a-number").startswith("Error:")


@respx.mock
async def test_send_message_is_blocked_outside_the_window(
    bridge: WhatsAppBridge, settings: Settings
) -> None:
    route = respx.post(settings.messages_url).mock(
        return_value=httpx.Response(200, json=SEND_RESPONSE)
    )
    stale = int(time.time()) - CUSTOMER_SERVICE_WINDOW_SECONDS - 60
    bridge.ingest(parse_webhook(inbound_payload(timestamp=stale)))

    result = await mcp_server.send_message("15550001111", "still there?")

    assert "Not sent" in result
    assert "send_template" in result
    # The point of the pre-check: no wasted API call.
    assert not route.called


@respx.mock
async def test_send_message_works_inside_the_window(
    bridge: WhatsAppBridge, settings: Settings
) -> None:
    respx.post(settings.messages_url).mock(
        return_value=httpx.Response(200, json=SEND_RESPONSE)
    )
    bridge.ingest(parse_webhook(inbound_payload()))

    result = await mcp_server.send_message("15550001111", "on it")

    assert "Sent to 15550001111" in result
    assert "wamid.SENT" in result


@respx.mock
async def test_send_message_to_an_unseen_contact_is_attempted(
    settings: Settings,
) -> None:
    """An empty store must not block sends -- Meta is the real authority."""
    route = respx.post(settings.messages_url).mock(
        return_value=httpx.Response(200, json=SEND_RESPONSE)
    )

    result = await mcp_server.send_message("15559998888", "hello")

    assert route.called
    assert "Sent to" in result


@respx.mock
async def test_send_message_surfaces_api_errors_as_text(
    bridge: WhatsAppBridge, settings: Settings
) -> None:
    respx.post(settings.messages_url).mock(
        return_value=httpx.Response(
            400,
            json={"error": {"code": 132000, "message": "Template param mismatch"}},
        )
    )
    bridge.ingest(parse_webhook(inbound_payload()))

    result = await mcp_server.send_message("15550001111", "hi")
    assert result.startswith("Send failed.")
    assert "132000" in result


async def test_send_media_validates_the_media_type() -> None:
    result = await mcp_server.send_media("15550001111", "hologram", link="https://x")
    assert result.startswith("Error: media_type must be one of")


@respx.mock
async def test_download_media_returns_a_path(settings: Settings) -> None:
    lookaside = "https://lookaside.test/asset"
    respx.get(settings.media_url("media-1")).mock(
        return_value=httpx.Response(
            200, json={"url": lookaside, "mime_type": "image/png", "id": "media-1"}
        )
    )
    respx.get(lookaside).mock(return_value=httpx.Response(200, content=b"png-bytes"))

    result = await mcp_server.download_media("media-1")
    assert result.startswith("Saved to ")
    assert result.endswith(".png")


def test_search_reports_no_matches_clearly(bridge: WhatsAppBridge) -> None:
    bridge.ingest(parse_webhook(inbound_payload(text="hello")))
    assert "No messages matching" in mcp_server.search_messages("refund")
    assert "hello" in mcp_server.search_messages("hello")
