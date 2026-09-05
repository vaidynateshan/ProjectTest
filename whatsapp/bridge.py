"""Service layer shared by the webhook and the MCP server.

Keeps two invariants in one place: every inbound event is persisted exactly
once, and every message we send is recorded so a thread shows both sides of
the conversation.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from .cloud_api import CloudAPIClient, MediaType
from .config import Settings
from .models import ParsedWebhook
from .store import ConversationStore, ThreadSummary

logger = logging.getLogger(__name__)

_NON_DIGITS = re.compile(r"\D")


def normalise_number(number: str) -> str:
    """Reduce a phone number to the digits-only form Meta uses as ``wa_id``.

    Users paste ``+1 (555) 000-1111``; the webhook reports ``15550001111``.
    Without this the same person yields two separate threads.
    """
    digits = _NON_DIGITS.sub("", number or "")
    if not digits:
        raise ValueError(f"{number!r} is not a usable phone number")
    return digits


def _sent_message_id(response: dict[str, Any]) -> str | None:
    messages = response.get("messages") or []
    if messages and isinstance(messages[0], dict):
        return messages[0].get("id")
    return None


class WhatsAppBridge:
    """Owns the store and the API client for one business phone number."""

    def __init__(
        self,
        settings: Settings,
        store: ConversationStore | None = None,
        client: CloudAPIClient | None = None,
    ) -> None:
        self.settings = settings
        self.store = store or ConversationStore(settings.db_path)
        self.client = client or CloudAPIClient(settings)

    async def aclose(self) -> None:
        await self.client.aclose()

    # -- inbound --------------------------------------------------------

    def ingest(self, parsed: ParsedWebhook) -> dict[str, int]:
        """Persist a parsed webhook. Safe to call twice with the same batch."""
        stored = duplicates = 0
        for message in parsed.messages:
            if self.store.save_message(message):
                stored += 1
            else:
                duplicates += 1

        for status in parsed.statuses:
            self.store.record_status(status)

        return {
            "messages_stored": stored,
            "messages_duplicate": duplicates,
            "statuses": len(parsed.statuses),
        }

    # -- outbound -------------------------------------------------------

    async def send_text(
        self, to: str, body: str, *, reply_to: str | None = None, preview_url: bool = False
    ) -> dict[str, Any]:
        wa_id = normalise_number(to)
        response = await self.client.send_text(
            wa_id, body, preview_url=preview_url, reply_to=reply_to
        )
        message_id = _sent_message_id(response)
        if message_id:
            self.store.save_outbound(
                message_id, wa_id, "text", body, reply_to=reply_to, raw=response
            )
        return response

    async def send_template(
        self,
        to: str,
        template_name: str,
        *,
        language_code: str = "en_US",
        body_params: list[str] | None = None,
    ) -> dict[str, Any]:
        wa_id = normalise_number(to)
        components = None
        if body_params:
            components = [
                {
                    "type": "body",
                    "parameters": [{"type": "text", "text": p} for p in body_params],
                }
            ]

        response = await self.client.send_template(
            wa_id,
            template_name,
            language_code=language_code,
            components=components,
        )
        message_id = _sent_message_id(response)
        if message_id:
            summary = f"template:{template_name}"
            if body_params:
                summary += f" ({', '.join(body_params)})"
            self.store.save_outbound(
                message_id, wa_id, "template", summary, raw=response
            )
        return response

    async def send_media(
        self,
        to: str,
        media_type: MediaType,
        *,
        link: str | None = None,
        media_id: str | None = None,
        caption: str | None = None,
        filename: str | None = None,
    ) -> dict[str, Any]:
        wa_id = normalise_number(to)
        response = await self.client.send_media(
            wa_id,
            media_type,
            link=link,
            media_id=media_id,
            caption=caption,
            filename=filename,
        )
        message_id = _sent_message_id(response)
        if message_id:
            self.store.save_outbound(
                message_id,
                wa_id,
                media_type,
                caption or link or media_id,
                media_id=media_id,
                raw=response,
            )
        return response

    async def download_media(self, media_id: str) -> Path:
        return await self.client.download_media(media_id, self.settings.media_dir)

    async def mark_read(self, message_id: str, *, typing: bool = False) -> dict[str, Any]:
        return await self.client.mark_read(message_id, typing=typing)

    # -- reads ----------------------------------------------------------

    def list_threads(self, limit: int = 20) -> list[ThreadSummary]:
        return self.store.list_threads(limit)

    def read_thread(self, wa_id: str, limit: int = 50) -> list[dict[str, Any]]:
        return self.store.read_thread(normalise_number(wa_id), limit)

    def thread(self, wa_id: str) -> ThreadSummary | None:
        return self.store.thread(normalise_number(wa_id))

    def search(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        return self.store.search_messages(query, limit)
