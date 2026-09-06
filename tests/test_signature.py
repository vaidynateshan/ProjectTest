from __future__ import annotations

from whatsapp.signature import compute_signature, verify_signature

SECRET = "test-app-secret"
BODY = b'{"object":"whatsapp_business_account"}'


def test_accepts_a_valid_signature() -> None:
    assert verify_signature(SECRET, BODY, compute_signature(SECRET, BODY))


def test_rejects_a_tampered_body() -> None:
    signature = compute_signature(SECRET, BODY)
    assert not verify_signature(SECRET, BODY + b" ", signature)


def test_rejects_a_signature_from_another_secret() -> None:
    assert not verify_signature(SECRET, BODY, compute_signature("other", BODY))


def test_rejects_missing_or_malformed_headers() -> None:
    digest = compute_signature(SECRET, BODY).removeprefix("sha256=")
    assert not verify_signature(SECRET, BODY, None)
    assert not verify_signature(SECRET, BODY, "")
    # Bare digest with no algorithm prefix must not pass.
    assert not verify_signature(SECRET, BODY, digest)
    assert not verify_signature(SECRET, BODY, f"sha1={digest}")


def test_tolerates_surrounding_whitespace_and_case() -> None:
    signature = compute_signature(SECRET, BODY)
    assert verify_signature(SECRET, BODY, f"  {signature.upper()}  ")
