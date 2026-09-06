"""Async client for the WhatsApp Business Cloud API.

Covers the endpoints a bridge needs: sending, read receipts, and the
two-step media download Meta requires.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any, Literal

import httpx

from .config import Settings

MediaType = Literal["image", "video", "audio", "document", "sticker"]

#: Meta rejects freeform sends outside the 24-hour customer service window
#: with this code. Worth naming: the generic message is easy to misread.
ERROR_OUTSIDE_WINDOW = 131047


class CloudAPIError(RuntimeError):
    """A structured error returned by the Graph API."""

    def __init__(
        self,
        status_code: int,
        code: int | None = None,
        message: str = "",
        details: str | None = None,
        fbtrace_id: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details
        self.fbtrace_id = fbtrace_id

        text = f"WhatsApp Cloud API error (HTTP {status_code}"
        if code is not None:
            text += f", code {code}"
        text += f"): {message}"
        if details:
            text += f" -- {details}"
        if code == ERROR_OUTSIDE_WINDOW:
            text += (
                "\nThe 24-hour customer service window has closed for this "
                "contact. Send an approved template instead."
            )
        super().__init__(text)

    @property
    def outside_window(self) -> bool:
        return self.code == ERROR_OUTSIDE_WINDOW


def _raise_for_error(response: httpx.Response) -> None:
    if response.status_code < 400:
        return
    try:
        error = (response.json() or {}).get("error") or {}
    except ValueError:
        error = {}
    raise CloudAPIError(
        status_code=response.status_code,
        code=error.get("code"),
        message=error.get("message") or response.text[:500],
        details=(error.get("error_data") or {}).get("details"),
        fbtrace_id=error.get("fbtrace_id"),
    )


class CloudAPIClient:
    """Thin wrapper over the Graph API message endpoints."""

    def __init__(
        self, settings: Settings, client: httpx.AsyncClient | None = None
    ) -> None:
        self.settings = settings
        self._client = client or httpx.AsyncClient(timeout=30.0)
        self._owns_client = client is None

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.settings.access_token}"}

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "CloudAPIClient":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def _post_message(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = await self._client.post(
            self.settings.messages_url, headers=self._headers, json=payload
        )
        _raise_for_error(response)
        return response.json()

    @staticmethod
    def _envelope(to: str, reply_to: str | None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
        }
        if reply_to:
            payload["context"] = {"message_id": reply_to}
        return payload

    # -- sending --------------------------------------------------------

    async def send_text(
        self,
        to: str,
        body: str,
        *,
        preview_url: bool = False,
        reply_to: str | None = None,
    ) -> dict[str, Any]:
        payload = self._envelope(to, reply_to)
        payload["type"] = "text"
        payload["text"] = {"body": body, "preview_url": preview_url}
        return await self._post_message(payload)

    async def send_template(
        self,
        to: str,
        template_name: str,
        *,
        language_code: str = "en_US",
        components: list[dict[str, Any]] | None = None,
        reply_to: str | None = None,
    ) -> dict[str, Any]:
        payload = self._envelope(to, reply_to)
        payload["type"] = "template"
        payload["template"] = {
            "name": template_name,
            "language": {"code": language_code},
        }
        if components:
            payload["template"]["components"] = components
        return await self._post_message(payload)

    async def send_media(
        self,
        to: str,
        media_type: MediaType,
        *,
        link: str | None = None,
        media_id: str | None = None,
        caption: str | None = None,
        filename: str | None = None,
        reply_to: str | None = None,
    ) -> dict[str, Any]:
        if bool(link) == bool(media_id):
            raise ValueError("Provide exactly one of link or media_id")

        media: dict[str, Any] = {"link": link} if link else {"id": media_id}
        # Audio and sticker messages reject a caption; document alone takes a
        # filename. Sending an unsupported field is a hard 400.
        if caption and media_type in ("image", "video", "document"):
            media["caption"] = caption
        if filename and media_type == "document":
            media["filename"] = filename

        payload = self._envelope(to, reply_to)
        payload["type"] = media_type
        payload[media_type] = media
        return await self._post_message(payload)

    async def mark_read(self, message_id: str, *, typing: bool = False) -> dict[str, Any]:
        """Mark an inbound message read, optionally showing a typing indicator."""
        payload: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": message_id,
        }
        if typing:
            payload["typing_indicator"] = {"type": "text"}
        return await self._post_message(payload)

    # -- media ----------------------------------------------------------

    async def get_media_metadata(self, media_id: str) -> dict[str, Any]:
        """Look up a media object. The returned ``url`` is valid for ~5 minutes."""
        response = await self._client.get(
            self.settings.media_url(media_id), headers=self._headers
        )
        _raise_for_error(response)
        return response.json()

    async def download_media(self, media_id: str, dest_dir: Path | None = None) -> Path:
        """Download media to disk and return the path.

        Two steps: resolve the id to a short-lived lookaside URL, then fetch
        it. The second request still needs the bearer token -- without it
        Meta returns a 403 that reads like the media does not exist.
        """
        metadata = await self.get_media_metadata(media_id)
        url = metadata.get("url")
        if not url:
            raise CloudAPIError(200, message=f"No download URL for media {media_id}")

        response = await self._client.get(url, headers=self._headers)
        _raise_for_error(response)

        target_dir = Path(dest_dir) if dest_dir else self.settings.media_dir
        target_dir.mkdir(parents=True, exist_ok=True)

        mime = metadata.get("mime_type", "").split(";")[0].strip()
        suffix = mimetypes.guess_extension(mime) or ""
        path = target_dir / f"{media_id}{suffix}"
        path.write_bytes(response.content)
        return path

    async def upload_media(
        self, path: Path | str, *, mime_type: str | None = None
    ) -> str:
        """Upload a local file and return its media id (reusable for 30 days)."""
        file_path = Path(path)
        resolved = mime_type or mimetypes.guess_type(file_path.name)[0]
        if not resolved:
            raise ValueError(f"Could not infer a MIME type for {file_path.name}")

        with file_path.open("rb") as handle:
            response = await self._client.post(
                self.settings.media_upload_url,
                headers=self._headers,
                data={"messaging_product": "whatsapp", "type": resolved},
                files={"file": (file_path.name, handle, resolved)},
            )
        _raise_for_error(response)
        return response.json()["id"]
