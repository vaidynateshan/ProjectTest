from __future__ import annotations

import httpx
import pytest
import respx

from whatsapp import doctor
from whatsapp.config import Settings


def test_valid_app_secret_passes() -> None:
    status, _ = doctor.check_app_secret("a" * 32)
    assert status == doctor.OK


def test_app_id_pasted_instead_of_secret_is_named_exactly() -> None:
    status, detail = doctor.check_app_secret("1234567890123456")
    assert status == doctor.BAD
    assert "App ID" in detail


def test_odd_length_secret_warns_without_blocking() -> None:
    status, _ = doctor.check_app_secret("short")
    assert status == doctor.WARN


def test_guessable_verify_token_warns() -> None:
    assert doctor.check_verify_token("changeme")[0] == doctor.WARN
    assert doctor.check_verify_token("token")[0] == doctor.WARN


def test_random_verify_token_passes() -> None:
    assert doctor.check_verify_token("02BSoORBWYYZCAYXhZkCUtYiCcxKn")[0] == doctor.OK


def test_successful_lookup_reports_the_sending_number() -> None:
    status, detail = doctor.interpret_phone_lookup(
        200,
        {
            "display_phone_number": "+1 555 999 8888",
            "verified_name": "Ada's Shop",
            "quality_rating": "GREEN",
        },
    )
    assert status == doctor.OK
    assert "+1 555 999 8888" in detail
    assert "Ada's Shop" in detail


def test_waba_id_mistake_is_diagnosed() -> None:
    # A WABA ID resolves to a real object, just not a phone number.
    status, detail = doctor.interpret_phone_lookup(200, {"id": "102290129340398"})
    assert status == doctor.BAD
    assert "WhatsApp Business Account ID" in detail


def test_expired_token_mentions_the_24_hour_expiry() -> None:
    status, detail = doctor.interpret_phone_lookup(
        401, {"error": {"code": 190, "message": "Session has expired"}}
    )
    assert status == doctor.BAD
    assert "24 hours" in detail


def test_missing_scopes_names_the_required_permissions() -> None:
    status, detail = doctor.interpret_phone_lookup(
        403, {"error": {"code": 200, "message": "Permissions error"}}
    )
    assert status == doctor.BAD
    assert "whatsapp_business_messaging" in detail


def test_unknown_id_points_at_the_right_dashboard_field() -> None:
    status, detail = doctor.interpret_phone_lookup(
        400,
        {
            "error": {
                "code": 100,
                "message": "Unsupported get request. Object does not exist",
            }
        },
    )
    assert status == doctor.BAD
    assert "Phone number ID" in detail
    assert "not found" in detail


def test_waba_id_used_as_phone_number_id_is_named_precisely() -> None:
    """The real failure seen in the field.

    Meta answers 400/100 for this, which reads like "ID not found" -- but the
    object exists and the token is fine. Saying "not found" sends people
    hunting for a typo instead of the adjacent field.
    """
    status, detail = doctor.interpret_phone_lookup(
        400,
        {
            "error": {
                "code": 100,
                "message": (
                    "(#100) Tried accessing nonexisting field "
                    "(display_phone_number) on node type "
                    "(WhatsAppBusinessAccount)"
                ),
            }
        },
    )
    assert status == doctor.BAD
    assert "WhatsAppBusinessAccount" in detail
    assert "token is working" in detail
    # Must NOT tell them the ID was not found -- it plainly was.
    assert "not found" not in detail


def test_wrong_object_type_detection() -> None:
    waba = {
        "error": {
            "code": 100,
            "message": "Tried accessing nonexisting field (display_phone_number)",
        }
    }
    missing = {"error": {"code": 100, "message": "Object does not exist"}}
    expired = {"error": {"code": 190, "message": "Session expired"}}

    assert doctor.is_wrong_object_type(waba) is True
    assert doctor.is_wrong_object_type(missing) is False
    assert doctor.is_wrong_object_type(expired) is False
    assert doctor.is_wrong_object_type({}) is False


@respx.mock
def test_discovery_lists_the_correct_phone_number_id(settings: Settings) -> None:
    """After diagnosing a WABA ID, hand back the ID that should be used."""
    respx.get(f"{settings.base_url}/{settings.phone_number_id}").mock(
        return_value=httpx.Response(
            400,
            json={
                "error": {
                    "code": 100,
                    "message": (
                        "Tried accessing nonexisting field "
                        "(display_phone_number) on node type "
                        "(WhatsAppBusinessAccount)"
                    ),
                }
            },
        )
    )
    respx.get(
        f"{settings.base_url}/{settings.phone_number_id}/phone_numbers"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "778899001122334",
                        "display_phone_number": "+1 555 999 8888",
                        "verified_name": "Ada's Shop",
                    }
                ]
            },
        )
    )

    status, detail = doctor.check_phone_number(settings)

    assert status == doctor.BAD
    assert "WHATSAPP_PHONE_NUMBER_ID=778899001122334" in detail
    assert "+1 555 999 8888" in detail


@respx.mock
def test_discovery_failure_is_explained_not_silent(settings: Settings) -> None:
    respx.get(f"{settings.base_url}/{settings.phone_number_id}").mock(
        return_value=httpx.Response(
            400,
            json={
                "error": {
                    "code": 100,
                    "message": "Tried accessing nonexisting field (display_phone_number)",
                }
            },
        )
    )
    respx.get(
        f"{settings.base_url}/{settings.phone_number_id}/phone_numbers"
    ).mock(return_value=httpx.Response(403, json={"error": {"code": 200}}))

    status, detail = doctor.check_phone_number(settings)

    assert status == doctor.BAD
    assert "Could not list phone numbers" in detail


@respx.mock
def test_check_phone_number_calls_the_graph_api(settings: Settings) -> None:
    route = respx.get(f"{settings.base_url}/{settings.phone_number_id}").mock(
        return_value=httpx.Response(
            200, json={"display_phone_number": "+1 555 999 8888"}
        )
    )

    status, _ = doctor.check_phone_number(settings)

    assert status == doctor.OK
    assert route.calls.last.request.headers["Authorization"] == "Bearer test-token"


@respx.mock
def test_network_failure_warns_rather_than_failing(settings: Settings) -> None:
    respx.get(f"{settings.base_url}/{settings.phone_number_id}").mock(
        side_effect=httpx.ConnectError("no route to host")
    )

    status, detail = doctor.check_phone_number(settings)

    # Being offline says nothing about whether the credentials are right.
    assert status == doctor.WARN
    assert "Could not reach" in detail
