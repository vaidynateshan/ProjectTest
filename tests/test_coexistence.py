"""Coexistence webhooks: the WhatsApp Business app and the API on one number.

Meta mirrors the app's activity to the API through two extra fields:
``smb_message_echoes`` for messages the business sends from its phone, and
``history`` for the backfill of conversations that existed before onboarding.
Without these the bridge sees only half of every conversation.
"""

from __future__ import annotations

import time
from dataclasses import replace

from whatsapp.bridge import WhatsAppBridge
from whatsapp.models import parse_webhook

NOW = int(time.time())


def _payload(field: str, **value: object) -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "1483325873634720",
                "changes": [
                    {
                        "field": field,
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "447880890177",
                                "phone_number_id": "1250501848154648",
                            },
                            **value,
                        },
                    }
                ],
            }
        ],
    }


# -- messages sent from the phone app -----------------------------------


def test_echo_is_recorded_as_outbound() -> None:
    parsed = parse_webhook(
        _payload(
            "smb_message_echoes",
            message_echoes=[
                {
                    "from": "447880890177",
                    "to": "447700900123",
                    "id": "wamid.ECHO1",
                    "timestamp": str(NOW),
                    "type": "text",
                    "text": {"body": "Yes, we're open until 6."},
                }
            ],
        )
    )

    assert len(parsed.messages) == 1
    message = parsed.messages[0]
    assert message.direction == "out"
    # The thread is the customer, not the business sending it.
    assert message.wa_id == "447700900123"
    assert message.text == "Yes, we're open until 6."


def test_echo_media_and_reply_context_survive() -> None:
    parsed = parse_webhook(
        _payload(
            "smb_message_echoes",
            message_echoes=[
                {
                    "from": "447880890177",
                    "to": "447700900123",
                    "id": "wamid.ECHO2",
                    "timestamp": str(NOW),
                    "type": "image",
                    "image": {"id": "media-55", "mime_type": "image/jpeg",
                              "caption": "here's the menu"},
                    "context": {"id": "wamid.CUSTOMER1"},
                }
            ],
        )
    )
    message = parsed.messages[0]
    assert message.direction == "out"
    assert message.media_id == "media-55"
    assert message.text == "here's the menu"
    assert message.reply_to == "wamid.CUSTOMER1"


# -- the history backfill -----------------------------------------------


def test_history_backfill_assigns_direction_per_message() -> None:
    """A backfilled thread contains both sides; the thread id is the customer."""
    parsed = parse_webhook(
        _payload(
            "history",
            contacts=[{"wa_id": "447700900123", "profile": {"name": "Priya"}}],
            history=[
                {
                    "metadata": {"phase": 0, "chunk_order": 1, "progress": 100},
                    "threads": [
                        {
                            "id": "447700900123",
                            "messages": [
                                {
                                    "from": "447700900123",
                                    "id": "wamid.H1",
                                    "timestamp": str(NOW - 3600),
                                    "type": "text",
                                    "text": {"body": "Are you open today?"},
                                },
                                {
                                    "from": "447880890177",
                                    "id": "wamid.H2",
                                    "timestamp": str(NOW - 3500),
                                    "type": "text",
                                    "text": {"body": "We are, until 6."},
                                },
                            ],
                        }
                    ],
                }
            ],
        )
    )

    assert len(parsed.messages) == 2
    first, second = parsed.messages
    assert (first.direction, first.text) == ("in", "Are you open today?")
    assert (second.direction, second.text) == ("out", "We are, until 6.")
    assert first.wa_id == second.wa_id == "447700900123"
    assert first.profile_name == "Priya"


def test_history_handles_several_threads_and_chunks() -> None:
    parsed = parse_webhook(
        _payload(
            "history",
            history=[
                {
                    "metadata": {"phase": 0, "chunk_order": 1},
                    "threads": [
                        {"id": "111", "messages": [
                            {"from": "111", "id": "a", "timestamp": str(NOW),
                             "type": "text", "text": {"body": "one"}}]},
                        {"id": "222", "messages": [
                            {"from": "222", "id": "b", "timestamp": str(NOW),
                             "type": "text", "text": {"body": "two"}}]},
                    ],
                },
                {
                    "metadata": {"phase": 0, "chunk_order": 2},
                    "threads": [
                        {"id": "111", "messages": [
                            {"from": "111", "id": "c", "timestamp": str(NOW),
                             "type": "text", "text": {"body": "three"}}]},
                    ],
                },
            ],
        )
    )
    assert {m.wa_id for m in parsed.messages} == {"111", "222"}
    assert len(parsed.messages) == 3


def test_malformed_history_is_skipped_not_raised() -> None:
    parsed = parse_webhook(
        _payload(
            "history",
            history=[
                None,
                "nonsense",
                {"threads": "not-a-list"},
                {"threads": [{"id": "111", "messages": [{"no": "id"}]}]},
            ],
        )
    )
    assert parsed.messages == []


# -- end to end through the bridge --------------------------------------


def test_a_backfilled_conversation_reads_as_a_conversation(
    bridge: WhatsAppBridge,
) -> None:
    """The payoff: both sides of an existing chat become readable."""
    bridge.ingest(
        parse_webhook(
            _payload(
                "history",
                contacts=[{"wa_id": "447700900123", "profile": {"name": "Priya"}}],
                history=[
                    {
                        "metadata": {"phase": 0, "chunk_order": 1},
                        "threads": [
                            {
                                "id": "447700900123",
                                "messages": [
                                    {"from": "447700900123", "id": "h1",
                                     "timestamp": str(NOW - 200), "type": "text",
                                     "text": {"body": "Do you deliver?"}},
                                    {"from": "447880890177", "id": "h2",
                                     "timestamp": str(NOW - 100), "type": "text",
                                     "text": {"body": "We do, within 5 miles."}},
                                ],
                            }
                        ],
                    }
                ],
            )
        )
    )
    # ... then a live reply typed on the phone afterwards.
    bridge.ingest(
        parse_webhook(
            _payload(
                "smb_message_echoes",
                message_echoes=[
                    {"from": "447880890177", "to": "447700900123", "id": "e1",
                     "timestamp": str(NOW), "type": "text",
                     "text": {"body": "Shall I book you in?"}},
                ],
            )
        )
    )

    thread = bridge.read_thread("447700900123")
    assert [(m["direction"], m["text"]) for m in thread] == [
        ("in", "Do you deliver?"),
        ("out", "We do, within 5 miles."),
        ("out", "Shall I book you in?"),
    ]
    assert bridge.thread("447700900123").profile_name == "Priya"


def test_history_redelivery_is_idempotent(bridge: WhatsAppBridge) -> None:
    """Chunks arrive out of order and can repeat; duplicates must not stack."""
    payload = _payload(
        "history",
        history=[{"metadata": {"chunk_order": 1}, "threads": [
            {"id": "447700900123", "messages": [
                {"from": "447700900123", "id": "h1", "timestamp": str(NOW),
                 "type": "text", "text": {"body": "hello"}}]}]}],
    )
    first = bridge.ingest(parse_webhook(payload))
    second = bridge.ingest(parse_webhook(payload))

    assert first["messages_stored"] == 1
    assert second["messages_duplicate"] == 1
    assert len(bridge.read_thread("447700900123")) == 1


# -- the onboarding page ------------------------------------------------


class TestOnboardingPage:
    """Embedded Signup is how a Business-app number joins the Cloud API."""

    def _client(self, settings, tmp_path):
        from fastapi.testclient import TestClient
        from whatsapp.bridge import WhatsAppBridge
        from whatsapp.cloud_api import CloudAPIClient
        from whatsapp.store import ConversationStore
        from whatsapp.webhook import create_app

        b = WhatsAppBridge(
            settings,
            store=ConversationStore(settings.db_path),
            client=CloudAPIClient(settings),
        )
        return TestClient(create_app(bridge=b))

    def test_page_carries_the_app_and_config_ids(self, settings, tmp_path):
        configured = replace(
            settings, app_id="1398155861925806", config_id="7788990011"
        )
        with self._client(configured, tmp_path) as client:
            response = client.get("/onboard")

        assert response.status_code == 200
        assert "1398155861925806" in response.text
        assert "7788990011" in response.text

    def test_page_requests_coexistence_not_a_new_number(self, settings, tmp_path):
        """featureType is the switch; without it Meta provisions a new number."""
        configured = replace(settings, app_id="123", config_id="456")
        with self._client(configured, tmp_path) as client:
            body = client.get("/onboard").text

        assert "whatsapp_business_app_onboarding" in body
        assert "sessionInfoVersion" in body

    def test_page_reminds_about_the_history_subscription(
        self, settings, tmp_path
    ):
        """History is sent once; missing the subscription loses it for good."""
        configured = replace(settings, app_id="123", config_id="456")
        with self._client(configured, tmp_path) as client:
            body = client.get("/onboard").text

        assert "history" in body
        assert "smb_message_echoes" in body

    def test_unconfigured_explains_what_is_missing(self, settings, tmp_path):
        with self._client(settings, tmp_path) as client:
            response = client.get("/onboard")

        assert response.status_code == 503
        assert "META_APP_ID" in response.text
        assert "META_CONFIG_ID" in response.text
