from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from whatsapp.bridge import WhatsAppBridge
from whatsapp.signature import SIGNATURE_HEADER, compute_signature
from whatsapp.webhook import create_app

from .conftest import APP_SECRET, VERIFY_TOKEN, inbound_payload


@pytest.fixture
def client(bridge: WhatsAppBridge):
    with TestClient(create_app(bridge=bridge)) as test_client:
        yield test_client


def post_signed(client: TestClient, payload: dict, *, secret: str = APP_SECRET):
    """POST a payload signed the way Meta signs it: over the exact bytes."""
    body = json.dumps(payload).encode()
    return client.post(
        "/webhook",
        content=body,
        headers={
            SIGNATURE_HEADER: compute_signature(secret, body),
            "Content-Type": "application/json",
        },
    )


# -- verification handshake ---------------------------------------------


def test_verification_echoes_the_challenge(client: TestClient) -> None:
    response = client.get(
        "/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": VERIFY_TOKEN,
            "hub.challenge": "1158201444",
        },
    )
    assert response.status_code == 200
    assert response.text == "1158201444"


def test_verification_rejects_a_wrong_token(client: TestClient) -> None:
    response = client.get(
        "/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "wrong",
            "hub.challenge": "123",
        },
    )
    assert response.status_code == 403


def test_verification_rejects_a_wrong_mode(client: TestClient) -> None:
    response = client.get(
        "/webhook",
        params={
            "hub.mode": "unsubscribe",
            "hub.verify_token": VERIFY_TOKEN,
            "hub.challenge": "123",
        },
    )
    assert response.status_code == 403


# -- event delivery ------------------------------------------------------


def test_signed_delivery_is_stored(client: TestClient, bridge: WhatsAppBridge) -> None:
    response = post_signed(client, inbound_payload(text="I need a refund"))

    assert response.status_code == 200
    assert response.json()["messages_stored"] == 1

    messages = bridge.read_thread("15550001111")
    assert len(messages) == 1
    assert messages[0]["text"] == "I need a refund"
    assert bridge.thread("15550001111").profile_name == "Ada Lovelace"


def test_unsigned_delivery_is_rejected_and_stores_nothing(
    client: TestClient, bridge: WhatsAppBridge
) -> None:
    response = client.post("/webhook", json=inbound_payload())

    assert response.status_code == 403
    assert bridge.list_threads() == []


def test_delivery_signed_with_the_wrong_secret_is_rejected(
    client: TestClient, bridge: WhatsAppBridge
) -> None:
    response = post_signed(client, inbound_payload(), secret="attacker-secret")

    assert response.status_code == 403
    assert bridge.list_threads() == []


def test_redelivery_of_the_same_message_is_idempotent(
    client: TestClient, bridge: WhatsAppBridge
) -> None:
    payload = inbound_payload(message_id="wamid.DUP")

    first = post_signed(client, payload)
    second = post_signed(client, payload)

    assert first.json()["messages_stored"] == 1
    assert second.json()["messages_stored"] == 0
    assert second.json()["messages_duplicate"] == 1
    assert len(bridge.read_thread("15550001111")) == 1


def test_malformed_body_answers_200_so_meta_stops_retrying(
    client: TestClient,
) -> None:
    body = b"this is not json"
    response = client.post(
        "/webhook",
        content=body,
        headers={
            SIGNATURE_HEADER: compute_signature(APP_SECRET, body),
            "Content-Type": "application/json",
        },
    )
    # A 5xx here would have Meta redeliver until it disables the webhook.
    assert response.status_code == 200
    assert response.json()["status"] == "error"


def test_status_updates_are_applied_to_sent_messages(
    client: TestClient, bridge: WhatsAppBridge
) -> None:
    bridge.store.save_outbound("wamid.OUT", "15550001111", "text", "hi")

    post_signed(
        client,
        {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "statuses": [
                                    {
                                        "id": "wamid.OUT",
                                        "recipient_id": "15550001111",
                                        "status": "read",
                                        "timestamp": str(int(time.time())),
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        },
    )

    assert bridge.store.get_message("wamid.OUT")["status"] == "read"


def test_health_reports_thread_count(client: TestClient) -> None:
    post_signed(client, inbound_payload())

    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["threads"] == 1
