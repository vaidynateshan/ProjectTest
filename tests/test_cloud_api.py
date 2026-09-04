from __future__ import annotations

import httpx
import pytest
import respx

from whatsapp.cloud_api import CloudAPIClient, CloudAPIError
from whatsapp.config import Settings

SEND_RESPONSE = {
    "messaging_product": "whatsapp",
    "contacts": [{"input": "15550001111", "wa_id": "15550001111"}],
    "messages": [{"id": "wamid.SENT"}],
}


@pytest.fixture
def client(settings: Settings) -> CloudAPIClient:
    return CloudAPIClient(settings)


@respx.mock
async def test_send_text_posts_the_expected_body(
    client: CloudAPIClient, settings: Settings
) -> None:
    route = respx.post(settings.messages_url).mock(
        return_value=httpx.Response(200, json=SEND_RESPONSE)
    )

    result = await client.send_text("15550001111", "Hello", reply_to="wamid.PREV")

    assert result["messages"][0]["id"] == "wamid.SENT"
    body = route.calls.last.request.read()
    import json

    sent = json.loads(body)
    assert sent["messaging_product"] == "whatsapp"
    assert sent["to"] == "15550001111"
    assert sent["type"] == "text"
    assert sent["text"] == {"body": "Hello", "preview_url": False}
    assert sent["context"] == {"message_id": "wamid.PREV"}
    assert route.calls.last.request.headers["Authorization"] == "Bearer test-token"


@respx.mock
async def test_send_template_includes_body_parameters(
    client: CloudAPIClient, settings: Settings
) -> None:
    import json

    route = respx.post(settings.messages_url).mock(
        return_value=httpx.Response(200, json=SEND_RESPONSE)
    )

    await client.send_template(
        "15550001111",
        "order_update",
        language_code="en_GB",
        components=[
            {"type": "body", "parameters": [{"type": "text", "text": "A123"}]}
        ],
    )

    sent = json.loads(route.calls.last.request.read())
    assert sent["type"] == "template"
    assert sent["template"]["name"] == "order_update"
    assert sent["template"]["language"] == {"code": "en_GB"}
    assert sent["template"]["components"][0]["parameters"][0]["text"] == "A123"


@respx.mock
async def test_audio_media_omits_caption(
    client: CloudAPIClient, settings: Settings
) -> None:
    import json

    route = respx.post(settings.messages_url).mock(
        return_value=httpx.Response(200, json=SEND_RESPONSE)
    )

    # Meta rejects a caption on audio with a hard 400, so it must be dropped.
    await client.send_media(
        "15550001111", "audio", link="https://x.test/a.mp3", caption="ignored"
    )
    sent = json.loads(route.calls.last.request.read())
    assert sent["audio"] == {"link": "https://x.test/a.mp3"}

    await client.send_media(
        "15550001111", "image", link="https://x.test/i.jpg", caption="kept"
    )
    sent = json.loads(route.calls.last.request.read())
    assert sent["image"]["caption"] == "kept"


async def test_send_media_requires_exactly_one_source(client: CloudAPIClient) -> None:
    with pytest.raises(ValueError):
        await client.send_media("15550001111", "image")
    with pytest.raises(ValueError):
        await client.send_media("15550001111", "image", link="u", media_id="m")


@respx.mock
async def test_outside_window_error_is_labelled(
    client: CloudAPIClient, settings: Settings
) -> None:
    respx.post(settings.messages_url).mock(
        return_value=httpx.Response(
            400,
            json={
                "error": {
                    "message": "(#131047) Re-engagement message",
                    "code": 131047,
                    "error_data": {"details": "More than 24 hours have passed"},
                    "fbtrace_id": "trace-1",
                }
            },
        )
    )

    with pytest.raises(CloudAPIError) as excinfo:
        await client.send_text("15550001111", "Hello")

    error = excinfo.value
    assert error.code == 131047
    assert error.outside_window is True
    assert error.fbtrace_id == "trace-1"
    assert "approved template" in str(error)


@respx.mock
async def test_non_json_error_body_still_raises_cleanly(
    client: CloudAPIClient, settings: Settings
) -> None:
    respx.post(settings.messages_url).mock(
        return_value=httpx.Response(502, text="<html>Bad Gateway</html>")
    )

    with pytest.raises(CloudAPIError) as excinfo:
        await client.send_text("15550001111", "Hello")
    assert excinfo.value.status_code == 502


@respx.mock
async def test_download_media_uses_two_steps_and_authenticates_both(
    client: CloudAPIClient, settings: Settings
) -> None:
    lookaside = "https://lookaside.test/asset?token=abc"
    meta_route = respx.get(settings.media_url("media-1")).mock(
        return_value=httpx.Response(
            200,
            json={
                "url": lookaside,
                "mime_type": "image/jpeg",
                "id": "media-1",
            },
        )
    )
    file_route = respx.get(lookaside).mock(
        return_value=httpx.Response(200, content=b"\xff\xd8binary")
    )

    path = await client.download_media("media-1")

    assert meta_route.called and file_route.called
    # The lookaside fetch fails with a confusing 403 without the bearer token.
    assert file_route.calls.last.request.headers["Authorization"] == "Bearer test-token"
    assert path.read_bytes() == b"\xff\xd8binary"
    assert path.suffix in {".jpg", ".jpeg"}


@respx.mock
async def test_mark_read_can_request_a_typing_indicator(
    client: CloudAPIClient, settings: Settings
) -> None:
    import json

    route = respx.post(settings.messages_url).mock(
        return_value=httpx.Response(200, json={"success": True})
    )

    await client.mark_read("wamid.IN", typing=True)
    sent = json.loads(route.calls.last.request.read())
    assert sent["status"] == "read"
    assert sent["message_id"] == "wamid.IN"
    assert sent["typing_indicator"] == {"type": "text"}
