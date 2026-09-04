"""MCP server exposing the WhatsApp bridge to Claude.

Read tools serve from the local store populated by the webhook; write tools
call the Cloud API and record what they sent. Run it over stdio:

    python -m whatsapp.mcp_server
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from mcp.server.mcpserver import MCPServer

from .bridge import WhatsAppBridge, normalise_number
from .cloud_api import CloudAPIError
from .config import load_settings
from .store import ThreadSummary

logger = logging.getLogger(__name__)

mcp = MCPServer(
    name="whatsapp-business",
    instructions=(
        "Read and send messages on a WhatsApp Business number via Meta's "
        "Cloud API. Conversations are read from a local store filled by the "
        "webhook, so only messages received while the webhook was running "
        "are visible. Freeform sends are only permitted within 24 hours of "
        "the contact's last message; outside that window use send_template."
    ),
)

_bridge: WhatsAppBridge | None = None


def get_bridge() -> WhatsAppBridge:
    """Lazily build the bridge so import never requires credentials."""
    global _bridge
    if _bridge is None:
        _bridge = WhatsAppBridge(load_settings())
    return _bridge


def _ts(value: int | None) -> str:
    if not value:
        return "unknown"
    return datetime.fromtimestamp(value, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _format_thread_line(summary: ThreadSummary) -> str:
    name = summary.profile_name or "unknown"
    window = (
        f"open until {_ts(summary.window_expires_at)}"
        if summary.window_open
        else "closed (template required)"
    )
    preview = (summary.last_message_preview or "").replace("\n", " ")
    if len(preview) > 80:
        preview = preview[:77] + "..."
    return (
        f"{summary.wa_id} | {name} | {summary.message_count} msgs | "
        f"last {_ts(summary.last_message_at)} | window {window}\n"
        f"    {preview}"
    )


@mcp.tool(
    description=(
        "List recent WhatsApp conversations, most recently active first. "
        "Shows each contact's number, name, message count, and whether the "
        "24-hour freeform reply window is still open."
    )
)
def list_threads(limit: int = 20) -> str:
    summaries = get_bridge().list_threads(limit)
    if not summaries:
        return (
            "No conversations stored yet. The webhook must be running and "
            "receiving messages before threads appear here."
        )
    return "\n".join(_format_thread_line(s) for s in summaries)


@mcp.tool(
    description=(
        "Read the message history of one conversation, oldest first. "
        "Accepts a phone number in any format."
    )
)
def read_thread(wa_id: str, limit: int = 50) -> str:
    bridge = get_bridge()
    try:
        messages = bridge.read_thread(wa_id, limit)
    except ValueError as exc:
        return f"Error: {exc}"

    if not messages:
        return f"No messages stored for {wa_id}."

    summary = bridge.thread(wa_id)
    header = ""
    if summary:
        window = (
            f"open until {_ts(summary.window_expires_at)}"
            if summary.window_open
            else "CLOSED -- freeform replies will be rejected, use send_template"
        )
        header = (
            f"Conversation with {summary.profile_name or 'unknown'} "
            f"({summary.wa_id})\n24-hour window: {window}\n\n"
        )

    lines = []
    for msg in messages:
        arrow = "<-" if msg["direction"] == "in" else "->"
        body = msg["text"] or f"<{msg['msg_type']}>"
        if msg["media_id"]:
            body += f" [media_id={msg['media_id']}]"
        detail = f" ({msg['status']})" if msg["status"] else ""
        if msg["error"]:
            detail += f" ERROR: {msg['error']}"
        lines.append(f"[{_ts(msg['timestamp'])}] {arrow} {body}{detail}")

    return header + "\n".join(lines)


@mcp.tool(description="Full-text search across stored WhatsApp messages.")
def search_messages(query: str, limit: int = 20) -> str:
    results = get_bridge().search(query, limit)
    if not results:
        return f"No messages matching {query!r}."
    return "\n".join(
        f"[{_ts(r['timestamp'])}] {r['wa_id']} "
        f"({r['profile_name'] or 'unknown'}) "
        f"{'<-' if r['direction'] == 'in' else '->'} {r['text']}"
        for r in results
    )


@mcp.tool(
    description=(
        "Send a freeform text message. Only works within 24 hours of the "
        "contact's last inbound message; outside that window use "
        "send_template. Optionally quote a message by its id."
    )
)
async def send_message(
    to: str, text: str, reply_to: str | None = None, preview_url: bool = False
) -> str:
    bridge = get_bridge()

    try:
        wa_id = normalise_number(to)
    except ValueError as exc:
        return f"Error: {exc}"

    # Pre-empt the round trip when we already know the window has closed.
    # A thread we have never seen is allowed through -- the store may simply
    # predate this contact, and Meta is the real authority.
    summary = bridge.store.thread(wa_id)
    if summary is not None and not summary.window_open:
        return (
            f"Not sent: the 24-hour window for {wa_id} closed at "
            f"{_ts(summary.window_expires_at)}. Use send_template with an "
            "approved template instead."
        )

    try:
        response = await bridge.send_text(
            wa_id, text, reply_to=reply_to, preview_url=preview_url
        )
    except CloudAPIError as exc:
        return f"Send failed. {exc}"

    message_id = (response.get("messages") or [{}])[0].get("id", "unknown")
    return f"Sent to {wa_id} (message id {message_id})."


@mcp.tool(
    description=(
        "Send a pre-approved WhatsApp message template. This is the only way "
        "to message a contact outside the 24-hour window. body_params fills "
        "the template's {{1}}, {{2}}... placeholders in order."
    )
)
async def send_template(
    to: str,
    template_name: str,
    language_code: str = "en_US",
    body_params: list[str] | None = None,
) -> str:
    bridge = get_bridge()
    try:
        response = await bridge.send_template(
            to,
            template_name,
            language_code=language_code,
            body_params=body_params,
        )
    except ValueError as exc:
        return f"Error: {exc}"
    except CloudAPIError as exc:
        return f"Send failed. {exc}"

    message_id = (response.get("messages") or [{}])[0].get("id", "unknown")
    return f"Template {template_name!r} sent to {to} (message id {message_id})."


@mcp.tool(
    description=(
        "Send a media message by public URL. media_type is one of image, "
        "video, audio, document, sticker. Subject to the same 24-hour window "
        "as freeform text."
    )
)
async def send_media(
    to: str,
    media_type: str,
    link: str,
    caption: str | None = None,
    filename: str | None = None,
) -> str:
    allowed = {"image", "video", "audio", "document", "sticker"}
    if media_type not in allowed:
        return f"Error: media_type must be one of {', '.join(sorted(allowed))}."

    try:
        response = await get_bridge().send_media(
            to, media_type, link=link, caption=caption, filename=filename  # type: ignore[arg-type]
        )
    except ValueError as exc:
        return f"Error: {exc}"
    except CloudAPIError as exc:
        return f"Send failed. {exc}"

    message_id = (response.get("messages") or [{}])[0].get("id", "unknown")
    return f"{media_type.capitalize()} sent to {to} (message id {message_id})."


@mcp.tool(
    description=(
        "Download an attachment a contact sent, by its media_id (shown in "
        "read_thread output). Returns the local file path."
    )
)
async def download_media(media_id: str) -> str:
    try:
        path = await get_bridge().download_media(media_id)
    except CloudAPIError as exc:
        return f"Download failed. {exc}"
    return f"Saved to {path.resolve()}"


@mcp.tool(
    description=(
        "Mark an inbound message as read (blue ticks), optionally showing a "
        "typing indicator to the contact."
    )
)
async def mark_read(message_id: str, typing: bool = False) -> str:
    try:
        await get_bridge().mark_read(message_id, typing=typing)
    except CloudAPIError as exc:
        return f"Failed. {exc}"
    return f"Marked {message_id} as read."


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    mcp.run("stdio")


if __name__ == "__main__":
    main()
