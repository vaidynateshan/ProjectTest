from __future__ import annotations

from whatsapp.models import parse_webhook

from .conftest import inbound_payload


def _value(**value: object) -> dict:
    return {"entry": [{"changes": [{"field": "messages", "value": value}]}]}


def test_parses_a_text_message_with_profile_name() -> None:
    parsed = parse_webhook(inbound_payload(text="Hi", name="Ada"))

    assert len(parsed.messages) == 1
    message = parsed.messages[0]
    assert message.text == "Hi"
    assert message.profile_name == "Ada"
    assert message.wa_id == "15550001111"
    assert message.msg_type == "text"
    assert message.timestamp > 0


def test_parses_media_caption_and_reply_context() -> None:
    parsed = parse_webhook(
        _value(
            messages=[
                {
                    "from": "15550001111",
                    "id": "wamid.IMG",
                    "timestamp": "1757000000",
                    "type": "image",
                    "image": {
                        "id": "media-99",
                        "mime_type": "image/jpeg",
                        "caption": "the invoice",
                    },
                    "context": {"id": "wamid.EARLIER"},
                }
            ]
        )
    )

    message = parsed.messages[0]
    assert message.media_id == "media-99"
    assert message.mime_type == "image/jpeg"
    assert message.text == "the invoice"
    assert message.reply_to == "wamid.EARLIER"


def test_parses_interactive_button_replies() -> None:
    parsed = parse_webhook(
        _value(
            messages=[
                {
                    "from": "15550001111",
                    "id": "wamid.BTN",
                    "timestamp": "1757000000",
                    "type": "interactive",
                    "interactive": {
                        "type": "button_reply",
                        "button_reply": {"id": "yes", "title": "Yes, please"},
                    },
                }
            ]
        )
    )
    assert parsed.messages[0].text == "Yes, please"


def test_unsupported_messages_keep_their_error_text() -> None:
    parsed = parse_webhook(
        _value(
            messages=[
                {
                    "from": "15550001111",
                    "id": "wamid.BAD",
                    "timestamp": "1757000000",
                    "type": "unsupported",
                    "errors": [{"code": 131051, "title": "Unsupported type"}],
                }
            ]
        )
    )
    assert parsed.messages[0].text == "[131051] Unsupported type"


def test_parses_failed_status_with_error_detail() -> None:
    parsed = parse_webhook(
        _value(
            statuses=[
                {
                    "id": "wamid.OUT",
                    "recipient_id": "15550001111",
                    "status": "failed",
                    "timestamp": "1757000000",
                    "errors": [
                        {
                            "code": 131047,
                            "title": "Re-engagement message",
                            "error_data": {"details": "Outside 24 hour window"},
                        }
                    ],
                }
            ]
        )
    )
    status = parsed.statuses[0]
    assert status.status == "failed"
    assert "131047" in (status.error or "")
    assert "Outside 24 hour window" in (status.error or "")


def test_malformed_entries_are_skipped_not_raised() -> None:
    # One good message alongside assorted junk Meta should never send but
    # which must not take the endpoint down if it does.
    payload = {
        "entry": [
            None,
            "nonsense",
            {"changes": [{"value": "not-a-dict"}]},
            {"changes": [{"value": {"messages": [{"no": "id"}]}}]},
            inbound_payload()["entry"][0],
        ]
    }
    parsed = parse_webhook(payload)
    assert len(parsed.messages) == 1


def test_empty_payload_is_falsy() -> None:
    assert not parse_webhook({})
    assert not parse_webhook({"entry": []})
