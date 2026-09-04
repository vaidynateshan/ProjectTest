from __future__ import annotations

import time

from whatsapp.models import InboundMessage, StatusUpdate
from whatsapp.store import CUSTOMER_SERVICE_WINDOW_SECONDS, ConversationStore

NOW = int(time.time())


def _message(**overrides: object) -> InboundMessage:
    defaults = dict(
        id="wamid.A",
        wa_id="15550001111",
        msg_type="text",
        timestamp=NOW,
        text="hello",
        profile_name="Ada",
        raw={"id": "wamid.A"},
    )
    defaults.update(overrides)
    return InboundMessage(**defaults)  # type: ignore[arg-type]


def test_saving_the_same_message_twice_is_a_no_op(store: ConversationStore) -> None:
    message = _message()
    assert store.save_inbound(message) is True
    assert store.save_inbound(message) is False
    assert len(store.read_thread("15550001111")) == 1


def test_thread_returns_messages_oldest_first(store: ConversationStore) -> None:
    store.save_inbound(_message(id="wamid.2", timestamp=NOW, text="second"))
    store.save_inbound(_message(id="wamid.1", timestamp=NOW - 60, text="first"))

    texts = [m["text"] for m in store.read_thread("15550001111")]
    assert texts == ["first", "second"]


def test_limit_keeps_the_most_recent_messages(store: ConversationStore) -> None:
    for index in range(5):
        store.save_inbound(
            _message(id=f"wamid.{index}", timestamp=NOW + index, text=f"msg{index}")
        )

    texts = [m["text"] for m in store.read_thread("15550001111", limit=2)]
    assert texts == ["msg3", "msg4"]


def test_status_update_applies_to_a_sent_message(store: ConversationStore) -> None:
    store.save_outbound("wamid.OUT", "15550001111", "text", "hi")
    store.record_status(
        StatusUpdate(
            message_id="wamid.OUT",
            wa_id="15550001111",
            status="read",
            timestamp=NOW,
        )
    )
    assert store.get_message("wamid.OUT")["status"] == "read"


def test_failed_status_keeps_its_error(store: ConversationStore) -> None:
    store.save_outbound("wamid.OUT", "15550001111", "text", "hi")
    store.record_status(
        StatusUpdate(
            message_id="wamid.OUT",
            wa_id="15550001111",
            status="failed",
            timestamp=NOW,
            error="[131047] Outside window",
        )
    )
    stored = store.get_message("wamid.OUT")
    assert stored["status"] == "failed"
    assert "131047" in stored["error"]


def test_status_for_an_unknown_message_creates_a_placeholder(
    store: ConversationStore,
) -> None:
    # Sent from the WhatsApp Manager UI rather than through this bridge.
    store.record_status(
        StatusUpdate(
            message_id="wamid.ELSEWHERE",
            wa_id="15550001111",
            status="delivered",
            timestamp=NOW,
        )
    )
    stored = store.get_message("wamid.ELSEWHERE")
    assert stored is not None
    assert stored["direction"] == "out"
    assert stored["status"] == "delivered"


def test_window_is_open_after_a_recent_inbound_message(store: ConversationStore) -> None:
    store.save_inbound(_message(timestamp=NOW - 60))
    summary = store.thread("15550001111")
    assert summary is not None
    assert summary.window_open is True
    assert summary.window_expires_at == NOW - 60 + CUSTOMER_SERVICE_WINDOW_SECONDS


def test_window_is_closed_after_25_hours(store: ConversationStore) -> None:
    store.save_inbound(_message(timestamp=NOW - CUSTOMER_SERVICE_WINDOW_SECONDS - 3600))
    assert store.thread("15550001111").window_open is False


def test_window_is_closed_when_only_outbound_messages_exist(
    store: ConversationStore,
) -> None:
    store.save_outbound("wamid.OUT", "15550001111", "text", "cold outreach")
    summary = store.thread("15550001111")
    assert summary.last_inbound_at is None
    assert summary.window_open is False


def test_threads_are_ordered_by_most_recent_activity(store: ConversationStore) -> None:
    store.save_inbound(_message(id="a", wa_id="111", timestamp=NOW - 100))
    store.save_inbound(_message(id="b", wa_id="222", timestamp=NOW))

    assert [t.wa_id for t in store.list_threads()] == ["222", "111"]


def test_thread_preview_describes_non_text_messages(store: ConversationStore) -> None:
    store.save_inbound(
        _message(id="wamid.IMG", msg_type="image", text=None, media_id="m1")
    )
    assert store.list_threads()[0].last_message_preview == "<image>"


def test_contact_name_is_not_overwritten_by_a_later_blank(
    store: ConversationStore,
) -> None:
    store.save_inbound(_message(id="wamid.1", profile_name="Ada"))
    store.save_inbound(_message(id="wamid.2", profile_name=None, timestamp=NOW + 10))
    assert store.thread("15550001111").profile_name == "Ada"


def test_search_finds_text_and_escapes_wildcards(store: ConversationStore) -> None:
    store.save_inbound(_message(id="wamid.1", text="refund please"))
    store.save_inbound(_message(id="wamid.2", text="100% happy"))

    assert [r["id"] for r in store.search_messages("refund")] == ["wamid.1"]
    # "%" must be matched literally. Unescaped it is a LIKE wildcard and
    # would return both rows instead of only the one containing a percent.
    assert [r["id"] for r in store.search_messages("100%")] == ["wamid.2"]
    assert [r["id"] for r in store.search_messages("%")] == ["wamid.2"]


def test_unknown_thread_returns_none(store: ConversationStore) -> None:
    assert store.thread("99999999") is None
    assert store.read_thread("99999999") == []


def test_messages_in_the_same_second_keep_insertion_order(
    store: ConversationStore,
) -> None:
    """Meta reports whole-second timestamps, so ties are common.

    Without a rowid tiebreaker a reply could render above the message it
    answers.
    """
    # All three share one timestamp, so only the rowid tiebreaker can order
    # them. Timestamps are set explicitly: stamping at call time would make
    # this race the second boundary.
    store.save_inbound(_message(id="wamid.1", timestamp=NOW, text="first"))
    store.save_inbound(_message(id="wamid.2", timestamp=NOW, text="second"))
    store.save_inbound(_message(id="wamid.3", timestamp=NOW, text="third"))

    for _ in range(5):
        texts = [m["text"] for m in store.read_thread("15550001111")]
        assert texts == ["first", "second", "third"]
