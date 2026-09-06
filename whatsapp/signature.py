"""Verification of Meta's ``X-Hub-Signature-256`` webhook signature.

Meta signs the raw request body with the app secret. The check must run
against the exact bytes received -- re-serialising the parsed JSON changes
key order and whitespace and will not match.
"""

from __future__ import annotations

import hashlib
import hmac

SIGNATURE_HEADER = "X-Hub-Signature-256"
_PREFIX = "sha256="


def compute_signature(app_secret: str, payload: bytes) -> str:
    """Return the header value Meta would send for ``payload``."""
    digest = hmac.new(app_secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return _PREFIX + digest


def verify_signature(app_secret: str, payload: bytes, header: str | None) -> bool:
    """Constant-time check of a received signature header."""
    if not header:
        return False

    received = header.strip().lower()
    if not received.startswith(_PREFIX):
        return False

    return hmac.compare_digest(compute_signature(app_secret, payload), received)
