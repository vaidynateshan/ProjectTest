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
        400, {"error": {"code": 100, "message": "Unsupported get request"}}
    )
    assert status == doctor.BAD
    assert "Phone number ID" in detail


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
