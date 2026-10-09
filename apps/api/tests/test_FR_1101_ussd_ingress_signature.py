"""FR-1101, the USSD ingress: nothing arrives unless the gateway signed it, signed or not.

The webhook takes a session id, a phone number and a keypress from its body, and does it
over the same shared secret as the SMS webhook. The signature is the first thing the route
checks, over the raw bytes rather than over a rendering of the parsed fields, and these
tests hold every way that check fails: no header, a wrong digest, a digest for other bytes,
and a body the form parser cannot read. A signed-but-incomplete form is the gateway's fault
and is a 422 envelope like any validation error.

Marked integration because every case goes through the real application and the real error
envelope. The route never reaches the database when it refuses, but the point is the reply,
and that is built by the application.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from fastapi.testclient import TestClient

from packages.shared.messaging.signature import sign_gateway_body

pytestmark = pytest.mark.integration

BODY_OK = b"sessionId=ses-sign-1&phoneNumber=%2B251911000001&text="


def _post(client: TestClient, raw: bytes, signature: str | None) -> httpx.Response:
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if signature is not None:
        headers["X-AT-Signature"] = signature
    response: httpx.Response = client.post("/v1/ussd/session", content=raw, headers=headers)
    return response


def test_a_body_with_no_signature_is_refused(client: TestClient) -> None:
    response = _post(client, BODY_OK, None)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "GATEWAY_SIGNATURE_INVALID"


def test_a_wrong_signature_is_refused(client: TestClient) -> None:
    response = _post(client, BODY_OK, "0" * 64)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "GATEWAY_SIGNATURE_INVALID"


def test_a_signature_for_other_bytes_is_refused(client: TestClient, gateway_secret: str) -> None:
    """The digest is over these bytes, not over some equivalent spelling of them."""

    elsewhere = b"text=&phoneNumber=%2B251911000001&sessionId=ses-sign-1"
    response = _post(client, BODY_OK, sign_gateway_body(gateway_secret, elsewhere))
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "GATEWAY_SIGNATURE_INVALID"


def test_a_signed_body_without_a_session_id_is_a_validation_error(
    client: TestClient, gateway_secret: str
) -> None:
    body = b"phoneNumber=%2B251911000001&text="
    response = _post(client, body, sign_gateway_body(gateway_secret, body))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_a_signed_body_without_a_phone_number_is_a_validation_error(
    client: TestClient, gateway_secret: str
) -> None:
    body = b"sessionId=ses-sign-1&text="
    response = _post(client, body, sign_gateway_body(gateway_secret, body))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_a_signed_body_that_is_not_urlencoded_utf8_is_a_validation_error(
    client: TestClient, gateway_secret: str
) -> None:
    body = b"\xff\xfe\x00 not utf-8 at all"
    response = _post(client, body, sign_gateway_body(gateway_secret, body))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_a_signed_first_press_is_answered_in_plain_text(
    client: TestClient,
    gateway_secret: str,
    ussd_session: Callable[..., httpx.Response],
) -> None:
    """An empty ``text`` is the opening dial, not a missing field: it is answered CON."""

    response = ussd_session(
        client,
        "",
        session_id="ses-sign-1",
        phone_number="+251911000001",
        secret=gateway_secret,
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert response.text.startswith("CON ")
    assert "Afaan Oromoo" in response.text
