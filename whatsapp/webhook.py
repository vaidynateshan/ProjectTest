"""FastAPI webhook receiving Cloud API events.

Meta requires two behaviours from this endpoint:

* ``GET`` must echo ``hub.challenge`` when the verify token matches, which is
  how a webhook subscription is first activated.
* ``POST`` must answer 200 quickly. Any other status is treated as a failure
  and redelivered, and sustained failures get the subscription disabled --
  so processing errors are logged and swallowed rather than returned.
"""

from __future__ import annotations

import hmac
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import APIRouter, FastAPI, Query, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from .bridge import WhatsAppBridge
from .config import Settings, load_settings
from .models import parse_webhook
from .signature import SIGNATURE_HEADER, verify_signature

logger = logging.getLogger(__name__)

router = APIRouter()


def _bridge(request: Request) -> WhatsAppBridge:
    return request.app.state.bridge


@router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    bridge = _bridge(request)
    return {
        "status": "ok",
        "phone_number_id": bridge.settings.phone_number_id,
        "api_version": bridge.settings.api_version,
        "threads": len(bridge.list_threads(limit=1000)),
    }


@router.get("/webhook")
async def verify(
    request: Request,
    mode: str | None = Query(None, alias="hub.mode"),
    token: str | None = Query(None, alias="hub.verify_token"),
    challenge: str | None = Query(None, alias="hub.challenge"),
) -> Response:
    """Handshake endpoint used once when the webhook is subscribed."""
    settings = _bridge(request).settings

    token_ok = token is not None and hmac.compare_digest(token, settings.verify_token)
    if mode == "subscribe" and token_ok and challenge is not None:
        logger.info("Webhook verification succeeded")
        return PlainTextResponse(challenge)

    logger.warning("Webhook verification failed (mode=%s, token_ok=%s)", mode, token_ok)
    return PlainTextResponse("Verification failed", status_code=403)


@router.post("/webhook")
async def receive(request: Request) -> Response:
    bridge = _bridge(request)
    raw_body = await request.body()

    if not verify_signature(
        bridge.settings.app_secret, raw_body, request.headers.get(SIGNATURE_HEADER)
    ):
        logger.warning("Rejected webhook with an invalid signature")
        return JSONResponse({"error": "invalid signature"}, status_code=403)

    try:
        payload = await request.json()
        parsed = parse_webhook(payload)
        result = bridge.ingest(parsed)
        if result["messages_stored"]:
            logger.info(
                "Stored %s message(s), %s status update(s)",
                result["messages_stored"],
                result["statuses"],
            )
        return JSONResponse({"status": "ok", **result})
    except Exception:
        # Deliberately broad: a 500 here makes Meta redeliver the same bad
        # payload until it disables the subscription entirely.
        logger.exception("Failed to process webhook payload")
        return JSONResponse({"status": "error"}, status_code=200)


def create_app(
    settings: Settings | None = None, bridge: WhatsAppBridge | None = None
) -> FastAPI:
    """Build the app. Tests inject a bridge; production loads from the env."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.bridge = bridge or WhatsAppBridge(settings or load_settings())
        try:
            yield
        finally:
            await app.state.bridge.aclose()

    app = FastAPI(
        title="WhatsApp Business bridge",
        description="Receives Cloud API webhooks and stores conversations.",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.include_router(router)
    return app


app = create_app  # uvicorn factory target: `uvicorn whatsapp.webhook:app --factory`
