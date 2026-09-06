from __future__ import annotations

import json
import time

import httpx
import pytest
import respx

from whatsapp.bridge import WhatsAppBridge, normalise_number
from whatsapp.config import Settings
from whatsapp.models import parse_webhook

from .conftest import inbound_payload

SEND_RESPONSE = {
    "messaging_product": "whatsapp",
    "contacts": [{"input": "15550001111", "wa_id": "15550001111"}],
    "messages": [{"id": "wamid.SENT"}],
}


@pytest.mark.parametrize(
    "raw",
    ["+1 (555) 000-1111", "1 555 000 1111", "15550001111", " +15550001111 "],
)
def test_numbers_normalise_to_the_webhook_form(raw: str) -> None:
    assert normalise_number(raw) == "15550001111"


def test_unusable_numbers_are_rejected() -> None:
    with pytest.raises(ValueError):
        normalise_number("no digits here")


def test_ingest_reports_stored_and_duplicate_counts(bridge: WhatsAppBridge) -> None:
    parsed = parse_webhook(inbound_payload(message_id="wamid.X"))

    assert bridge.ingest(parsed)["messages_stored"] == 1
    assert bridge.ingest(parsed)["messages_duplicate"] == 1


@respx.mock
async def test_sent_messages_join_the_same_thread_as_inbound(
    bridge: WhatsAppBridge, settings: Settings
) -> None:
    respx.post(settings.messages_url).mock(
        return_value=httpx.Response(200, json=SEND_RESPONSE)
    )
    bridge.ingest(parse_webhook(inbound_payload(text="hi")))

    # Sent with punctuation the customer's wa_id does not have.
    await bridge.send_text("+1 (555) 000-1111", "Thanks for reaching out")

    thread = bridge.read_thread("15550001111")
    assert [(m["direction"], m["text"]) for m in thread] == [
        ("in", "hi"),
        ("out", "Thanks for reaching out"),
    ]
    assert len(bridge.list_threads()) == 1


@respx.mock
async def test_template_sends_are_recorded_with_their_parameters(
    bridge: WhatsAppBridge, settings: Settings
) -> None:
    route = respx.post(settings.messages_url).mock(
        return_value=httpx.Response(200, json=SEND_RESPONSE)
    )

    await bridge.send_template(
        "15550001111", "order_update", body_params=["A123", "shipped"]
    )

    sent = json.loads(route.calls.last.request.read())
    params = sent["template"]["components"][0]["parameters"]
    assert [p["text"] for p in params] == ["A123", "shipped"]

    stored = bridge.read_thread("15550001111")[0]
    assert stored["msg_type"] == "template"
    assert "order_update" in stored["text"]
    assert "A123" in stored["text"]


@respx.mock
async def test_a_failed_send_is_not_recorded_as_delivered(
    bridge: WhatsAppBridge, settings: Settings
) -> None:
    respx.post(settings.messages_url).mock(
        return_value=httpx.Response(
            400, json={"error": {"code": 131047, "message": "outside window"}}
        )
    )

    from whatsapp.cloud_api import CloudAPIError

    with pytest.raises(CloudAPIError):
        await bridge.send_text("15550001111", "hello")

    # Nothing should be written for a send the API rejected.
    assert bridge.read_thread("15550001111") == []
