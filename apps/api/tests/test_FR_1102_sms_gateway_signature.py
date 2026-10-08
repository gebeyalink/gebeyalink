"""FR-1102, the gateway's signature: nobody who cannot sign gets to name a sender.

The webhook takes a phone number and a command from its body, so a route that acted on an
unsigned body would let anyone who can reach the port post listings, place offers and spend
action tokens as whichever number they typed. The signature is therefore the first thing the
route checks, over the raw bytes rather than over a rendering of the parsed fields, and these
tests hold every way that check can fail: no header, a wrong digest, and a body that is not a
form at all.

Marked integration because every case goes through the real application and the real error
envelope. An unsigned request never reaches the database, but the point is the response, and
that is built by the application.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from apps.api.providers.sms import RecordingSmsProvider
from packages.shared.messaging.signature import sign_gateway_body

pytestmark = pytest.mark.integration


def _post_signed(client: TestClient, body: bytes, signature: str | None) -> httpx.Response:
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if signature is not None:
        headers["X-AT-Signature"] = signature
    response: httpx.Response = client.post("/v1/sms/inbound", content=body, headers=headers)
    return response


def test_a_body_with_no_signature_is_refused(client: TestClient) -> None:
    body = b"from=%2B251911000001&text=HELP"
    response = _post_signed(client, body, None)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "GATEWAY_SIGNATURE_INVALID"


def test_a_wrong_signature_is_refused(client: TestClient) -> None:
    body = b"from=%2B251911000001&text=HELP"
    response = _post_signed(client, body, "0" * 64)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "GATEWAY_SIGNATURE_INVALID"


def test_a_signature_for_other_bytes_is_refused(client: TestClient, gateway_secret: str) -> None:
    """The digest is over these bytes, not over some equivalent rendering of them."""

    body = b"from=%2B251911000001&text=HELP"
    elsewhere = b"text=HELP&from=%2B251911000001"
    response = _post_signed(client, body, sign_gateway_body(gateway_secret, elsewhere))
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "GATEWAY_SIGNATURE_INVALID"


def test_a_body_that_is_not_a_form_is_a_validation_error(
    client: TestClient, gateway_secret: str
) -> None:
    """Signed, so the gateway is not lying, but not a command: 422 rather than a refusal."""

    body = b"\xff\xfe\x00 not utf-8 at all"
    response = _post_signed(client, body, sign_gateway_body(gateway_secret, body))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_a_signed_but_incomplete_form_is_a_validation_error(
    client: TestClient, gateway_secret: str
) -> None:
    body = b"text=HELP"
    response = _post_signed(client, body, sign_gateway_body(gateway_secret, body))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_a_signed_request_is_answered_with_an_empty_200(
    client: TestClient,
    gateway_secret: str,
    sms_recorder: RecordingSmsProvider,
) -> None:
    body = b"from=%2B251911000001&text=HELP"
    response = _post_signed(client, body, sign_gateway_body(gateway_secret, body))
    assert response.status_code == 200
    assert response.content == b""
    assert sms_recorder.messages_to("+251911000001")
