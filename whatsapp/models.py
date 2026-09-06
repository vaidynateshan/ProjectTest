"""Normalisation of Cloud API webhook payloads.

Meta nests everything under ``entry[].changes[].value`` and gives each
message type its own shape. This module flattens that into two flat record
types so the rest of the codebase never touches the raw envelope.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

#: Message types that carry a downloadable media object.
MEDIA_TYPES = frozenset({"image", "video", "audio", "document", "sticker"})


@dataclass
class InboundMessage:
    """One message in a conversation.

    Named for the common case, but Coexistence also delivers messages the
    business itself sent from the WhatsApp Business app, so ``direction``
    distinguishes them.
    """

    id: str
    wa_id: str
    msg_type: str
    timestamp: int
    #: "in" from the customer, "out" sent by the business.
    direction: str = "in"
    text: str | None = None
    media_id: str | None = None
    mime_type: str | None = None
    filename: str | None = None
    reply_to: str | None = None
    profile_name: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def raw_json(self) -> str:
        return json.dumps(self.raw, separators=(",", ":"), sort_keys=True)


@dataclass
class StatusUpdate:
    """A delivery receipt for a message the business sent."""

    message_id: str
    wa_id: str
    status: str
    timestamp: int
    error: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def raw_json(self) -> str:
        return json.dumps(self.raw, separators=(",", ":"), sort_keys=True)


@dataclass
class ParsedWebhook:
    messages: list[InboundMessage] = field(default_factory=list)
    statuses: list[StatusUpdate] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.messages or self.statuses)


def _as_int(value: Any, default: int = 0) -> int:
    """Meta sends epoch seconds as a string; be forgiving about it."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _describe_errors(errors: list[dict[str, Any]] | None) -> str | None:
    if not errors:
        return None
    parts = []
    for err in errors:
        code = err.get("code")
        title = err.get("title") or err.get("message") or ""
        detail = (err.get("error_data") or {}).get("details")
        text = f"[{code}] {title}" if code else title
        if detail:
            text = f"{text}: {detail}"
        parts.append(text.strip())
    return "; ".join(p for p in parts if p) or None


def _extract_content(message: dict[str, Any]) -> tuple[str | None, str | None, str | None, str | None]:
    """Return ``(text, media_id, mime_type, filename)`` for any message type.

    Each type stores its human-readable content somewhere different; callers
    want one ``text`` field they can display in a thread.
    """
    msg_type = message.get("type", "")
    body = message.get(msg_type)

    if msg_type == "text":
        return (message.get("text", {}).get("body"), None, None, None)

    if msg_type in MEDIA_TYPES:
        media = body if isinstance(body, dict) else {}
        return (
            media.get("caption"),
            media.get("id"),
            media.get("mime_type"),
            media.get("filename"),
        )

    if msg_type == "location":
        loc = body if isinstance(body, dict) else {}
        label = ", ".join(
            str(part) for part in (loc.get("name"), loc.get("address")) if part
        )
        coords = f"{loc.get('latitude')},{loc.get('longitude')}"
        return (f"{label} ({coords})" if label else coords, None, None, None)

    if msg_type == "button":
        return ((body or {}).get("text"), None, None, None)

    if msg_type == "interactive":
        interactive = body if isinstance(body, dict) else {}
        reply = interactive.get("button_reply") or interactive.get("list_reply") or {}
        return (reply.get("title") or reply.get("description"), None, None, None)

    if msg_type == "reaction":
        reaction = body if isinstance(body, dict) else {}
        return (reaction.get("emoji"), None, None, None)

    if msg_type == "contacts":
        contacts = body if isinstance(body, list) else []
        names = [
            (c.get("name") or {}).get("formatted_name", "") for c in contacts
        ]
        return (", ".join(n for n in names if n) or None, None, None, None)

    if msg_type == "order":
        order = body if isinstance(body, dict) else {}
        items = order.get("product_items") or []
        return (f"Order with {len(items)} item(s)", None, None, None)

    # ``unsupported`` and anything Meta adds after this was written: keep the
    # error text so the thread shows *something* rather than a silent gap.
    return (_describe_errors(message.get("errors")), None, None, None)


def parse_webhook(payload: dict[str, Any]) -> ParsedWebhook:
    """Flatten a webhook body into messages and status updates.

    Unknown or malformed entries are skipped rather than raising: a webhook
    handler must not 500 on one bad record inside an otherwise valid batch.
    """
    result = ParsedWebhook()

    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue

        for change in entry.get("changes") or []:
            if not isinstance(change, dict):
                continue
            value = change.get("value")
            if not isinstance(value, dict):
                continue

            # ``contacts`` carries the sender's WhatsApp profile name, which
            # only ever appears alongside inbound messages.
            names: dict[str, str] = {}
            for contact in value.get("contacts") or []:
                wa_id = contact.get("wa_id")
                name = (contact.get("profile") or {}).get("name")
                if wa_id and name:
                    names[wa_id] = name

            for message in value.get("messages") or []:
                if not isinstance(message, dict) or not message.get("id"):
                    continue
                wa_id = message.get("from") or ""
                text, media_id, mime_type, filename = _extract_content(message)
                result.messages.append(
                    InboundMessage(
                        id=message["id"],
                        wa_id=wa_id,
                        msg_type=message.get("type") or "unknown",
                        timestamp=_as_int(message.get("timestamp")),
                        text=text,
                        media_id=media_id,
                        mime_type=mime_type,
                        filename=filename,
                        reply_to=(message.get("context") or {}).get("id"),
                        profile_name=names.get(wa_id),
                        raw=message,
                    )
                )

            # Coexistence: messages the business sent from the WhatsApp
            # Business app on their phone, mirrored back to us.
            for echo in value.get("message_echoes") or []:
                if not isinstance(echo, dict) or not echo.get("id"):
                    continue
                text, media_id, mime_type, filename = _extract_content(echo)
                result.messages.append(
                    InboundMessage(
                        id=echo["id"],
                        # The thread is keyed on the customer, who is the
                        # recipient when the business is the sender.
                        wa_id=echo.get("to") or "",
                        msg_type=echo.get("type") or "unknown",
                        timestamp=_as_int(echo.get("timestamp")),
                        direction="out",
                        text=text,
                        media_id=media_id,
                        mime_type=mime_type,
                        filename=filename,
                        reply_to=(echo.get("context") or {}).get("id"),
                        raw=echo,
                    )
                )

            # Coexistence: the backfill of conversations that already existed
            # in the app, delivered in chunks after onboarding.
            for chunk in value.get("history") or []:
                if not isinstance(chunk, dict):
                    continue
                for thread in chunk.get("threads") or []:
                    if not isinstance(thread, dict):
                        continue
                    thread_id = thread.get("id") or ""
                    for message in thread.get("messages") or []:
                        if not isinstance(message, dict) or not message.get("id"):
                            continue
                        text, media_id, mime_type, filename = _extract_content(message)
                        sender = message.get("from") or ""
                        result.messages.append(
                            InboundMessage(
                                id=message["id"],
                                wa_id=thread_id,
                                msg_type=message.get("type") or "unknown",
                                timestamp=_as_int(message.get("timestamp")),
                                # In a backfilled thread the customer is the
                                # thread id, so anyone else is the business.
                                direction="in" if sender == thread_id else "out",
                                text=text,
                                media_id=media_id,
                                mime_type=mime_type,
                                filename=filename,
                                reply_to=(message.get("context") or {}).get("id"),
                                profile_name=names.get(thread_id),
                                raw=message,
                            )
                        )

            for status in value.get("statuses") or []:
                if not isinstance(status, dict) or not status.get("id"):
                    continue
                result.statuses.append(
                    StatusUpdate(
                        message_id=status["id"],
                        wa_id=status.get("recipient_id") or "",
                        status=status.get("status") or "unknown",
                        timestamp=_as_int(status.get("timestamp")),
                        error=_describe_errors(status.get("errors")),
                        raw=status,
                    )
                )

    return result
