"""Check that the four credentials in .env are actually correct.

Run it after filling in .env:

    python -m whatsapp.doctor

Two of these values have near-identical decoys in Meta's dashboard (the
WhatsApp Business Account ID looks just like the phone number ID, and the
app ID sits next to the app secret), so a live call is the only honest way
to confirm what you pasted.
"""

from __future__ import annotations

import os
import re
import sys
from typing import Any

import httpx

from .config import ConfigError, Settings, load_settings

OK = "PASS"
BAD = "FAIL"
WARN = "WARN"

#: Meta app secrets are 32 lowercase hex characters.
_APP_SECRET_RE = re.compile(r"^[0-9a-f]{32}$")

#: Meta names the object it actually found, e.g.
#: "... on node type (WhatsAppBusinessAccount)". Worth surfacing: it is the
#: difference between "your ID is wrong" and "your ID is the wrong kind".
_NODE_TYPE_RE = re.compile(r"on node type \(([^)]+)\)")

_WEAK_VERIFY_TOKENS = {
    "", "token", "verify", "test", "changeme", "secret", "password",
    "your-verify-token", "whatsapp",
}


def _line(status: str, label: str, detail: str) -> str:
    return f"  [{status}] {label}\n         {detail}"


def check_app_secret(app_secret: str) -> tuple[str, str]:
    """Format-check the app secret.

    It cannot be validated against the API without the app ID, but the usual
    mistake -- pasting the App ID, which is a short decimal number sitting
    directly above it -- is caught by the shape.
    """
    if _APP_SECRET_RE.match(app_secret):
        return OK, "Looks like a valid app secret (32 hex characters)."
    if app_secret.isdigit():
        return BAD, (
            "This is all digits, so it is almost certainly the App ID, not "
            "the App Secret. They sit next to each other on App Settings > "
            "Basic; the secret is hidden behind a 'Show' button."
        )
    return WARN, (
        f"Expected 32 hex characters, got {len(app_secret)}. Meta may have "
        "changed the format, but double-check you copied the App Secret."
    )


def check_verify_token(verify_token: str) -> tuple[str, str]:
    if verify_token.lower() in _WEAK_VERIFY_TOKENS:
        return WARN, (
            "This is a guessable value. It is the only thing stopping a "
            "stranger from re-pointing your webhook subscription. Use a "
            "random string instead."
        )
    if len(verify_token) < 16:
        return WARN, f"Only {len(verify_token)} characters; prefer 32 or more."
    return OK, "Present and suitably random."


def interpret_phone_lookup(
    status_code: int, body: dict[str, Any]
) -> tuple[str, str]:
    """Turn the Graph API's response into a specific, actionable message."""
    if status_code == 200:
        number = body.get("display_phone_number")
        if not number:
            return BAD, (
                "That ID exists but is not a phone number. You have most "
                "likely pasted the WhatsApp Business Account ID, which is "
                "shown right beside the phone number ID on WhatsApp > API "
                "Setup. You want the one labelled 'Phone number ID'."
            )
        name = body.get("verified_name") or "unnamed"
        quality = body.get("quality_rating") or "unknown"
        return OK, (
            f"Token and phone number ID both work. Sending as "
            f"{number} ({name}), quality rating {quality}."
        )

    error = body.get("error") or {}
    code = error.get("code")
    message = error.get("message") or f"HTTP {status_code}"

    if code == 190:
        return BAD, (
            f"The access token was rejected: {message}\n         "
            "If you used the temporary token from API Setup, note it expires "
            "24 hours after it was issued. Generate a permanent System User "
            "token for anything beyond a first test."
        )
    if code in (200, 10, 3):
        return BAD, (
            f"The token is valid but lacks permission: {message}\n         "
            "The token needs the whatsapp_business_messaging and "
            "whatsapp_business_management scopes, and the System User must "
            "have the WhatsApp Business Account assigned as an asset."
        )
    if code == 100:
        # Two very different situations share this code. "Nonexisting field"
        # means the ID resolved to a real object that simply is not a phone
        # number -- almost always the WhatsApp Business Account ID, which sits
        # directly beside the phone number ID in the dashboard.
        if is_wrong_object_type(body):
            node = _NODE_TYPE_RE.search(message)
            found = node.group(1) if node else "something other than a phone number"
            return BAD, (
                f"That ID exists, but it is {found} -- not a phone number.\n         "
                "Your access token is working; only the ID is wrong. Copy "
                "'Phone number ID' from WhatsApp > API Setup, which sits above "
                "the WhatsApp Business Account ID."
            )
        return BAD, (
            f"The phone number ID was not found: {message}\n         "
            "Check you copied 'Phone number ID' from WhatsApp > API Setup -- "
            "not the phone number itself, and not the WhatsApp Business "
            "Account ID directly below it."
        )
    return BAD, f"Unexpected error (code {code}): {message}"


def is_wrong_object_type(body: dict[str, Any]) -> bool:
    """True when the ID resolved but is not a phone number object."""
    error = body.get("error") or {}
    if error.get("code") != 100:
        return False
    return "nonexisting field" in (error.get("message") or "").lower()


def discover_phone_numbers(
    settings: Settings, account_id: str
) -> list[dict[str, Any]]:
    """List the phone numbers under a WhatsApp Business Account.

    Used to hand back the correct ID once we know the configured one is a
    WABA ID -- hunting for it in the dashboard is what goes wrong.
    """
    try:
        response = httpx.get(
            f"{settings.base_url}/{account_id}/phone_numbers",
            params={"fields": "id,display_phone_number,verified_name"},
            headers={"Authorization": f"Bearer {settings.access_token}"},
            timeout=15.0,
        )
        if response.status_code != 200:
            return []
        return response.json().get("data") or []
    except (httpx.RequestError, ValueError):
        return []


def check_phone_number(settings: Settings) -> tuple[str, str]:
    url = f"{settings.base_url}/{settings.phone_number_id}"
    try:
        response = httpx.get(
            url,
            params={
                "fields": "display_phone_number,verified_name,quality_rating"
            },
            headers={"Authorization": f"Bearer {settings.access_token}"},
            timeout=15.0,
        )
    except httpx.RequestError as exc:
        return WARN, f"Could not reach the Graph API: {exc}"

    try:
        body = response.json()
    except ValueError:
        body = {}

    status, detail = interpret_phone_lookup(response.status_code, body)

    # When the ID turns out to be a WABA, ask Meta for the phone numbers under
    # it so the correct value can be pasted straight in.
    if status == BAD and is_wrong_object_type(body):
        numbers = discover_phone_numbers(settings, settings.phone_number_id)
        if numbers:
            lines = "\n".join(
                f"           WHATSAPP_PHONE_NUMBER_ID={n.get('id')}"
                f"   ({n.get('display_phone_number', '?')}"
                f" - {n.get('verified_name', 'unnamed')})"
                for n in numbers
            )
            detail += (
                "\n\n         Found these phone numbers on that account -- "
                "put one in your .env:\n" + lines
            )
        else:
            detail += (
                "\n\n         Could not list phone numbers on that account. "
                "The token may lack whatsapp_business_management, or no "
                "number is registered yet."
            )

    return status, detail


def main() -> int:
    print("Checking WhatsApp Business credentials\n")

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"  [{BAD}] Configuration\n         {exc}")
        print("\nFill in .env and run this again.")
        return 1

    results = [
        ("App secret", *check_app_secret(settings.app_secret)),
        ("Verify token", *check_verify_token(settings.verify_token)),
        ("Access token + phone number ID", *check_phone_number(settings)),
    ]

    for label, status, detail in results:
        print(_line(status, label, detail))
        print()

    statuses = {status for _, status, _ in results}
    if BAD in statuses:
        print("Something is wrong above. Fix it before subscribing the webhook.")
        return 1
    if WARN in statuses:
        print("Usable, but review the warnings above.")
        return 0

    print("All good. Next: start the webhook and subscribe it in the dashboard.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
