"""FR-104: the two halves of a signed-in session.

An access token is short and self-contained. A refresh token is long, and that is exactly why
it is single-use: it is the one credential worth stealing, so each one works once and the
trading of it is recorded. Both are signed rather than encrypted, which means a token that
leaves the server is readable but useless to alter.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import jwt
import pytest
from fastapi.testclient import TestClient
from redis import Redis as SyncRedis

pytestmark = pytest.mark.integration


def _decode(token: str, public_key: str) -> dict[str, Any]:
    """Check a token's signature the way a resource server would, then look inside."""
    claims: dict[str, Any] = jwt.decode(token, public_key, algorithms=["RS256"])
    return claims


def test_FR_104_a_signed_in_trader_receives_two_signed_tokens(
    client: TestClient,
    signing_keys: tuple[str, str],
    sign_in: Callable[..., httpx.Response],
) -> None:
    """Both halves are RS256-signed, and neither carries anything worth stealing."""
    public_key = signing_keys[1]
    body = sign_in("+251955555555").json()

    access = _decode(body["access_token"], public_key)
    refresh = _decode(body["refresh_token"], public_key)

    assert access["kind"] == "access"
    assert refresh["kind"] == "refresh"
    assert access["kid"] == refresh["kid"] == "test-key"
    assert access["sub"] == refresh["sub"] == body["user"]["id"]
    assert "phone_e164" not in access
    assert "language" not in access


def test_FR_104_the_access_token_lasts_fifteen_minutes(
    client: TestClient, signing_keys: tuple[str, str], sign_in: Callable[..., httpx.Response]
) -> None:
    """The short half is short: a quarter of an hour, as specified."""
    body = sign_in("+251955555556").json()

    claims = _decode(body["access_token"], signing_keys[1])
    issued = datetime.fromtimestamp(int(claims["iat"]), UTC)
    expires = datetime.fromtimestamp(int(claims["exp"]), UTC)

    assert expires - issued == timedelta(minutes=15)
    # The advertised moment and the signed one agree to the second: the token says "valid
    # for fifteen minutes" and the response must not tell the client anything different.
    advertised = datetime.fromisoformat(body["access_expires_at"])
    assert abs((advertised - expires).total_seconds()) < 1


def test_FR_104_the_refresh_token_lasts_thirty_days(
    client: TestClient, signing_keys: tuple[str, str], sign_in: Callable[..., httpx.Response]
) -> None:
    """The long half is long, but still finite and still signed."""
    body = sign_in("+251955555557").json()

    claims = _decode(body["refresh_token"], signing_keys[1])
    issued = datetime.fromtimestamp(int(claims["iat"]), UTC)
    expires = datetime.fromtimestamp(int(claims["exp"]), UTC)

    assert expires - issued == timedelta(days=30)
    advertised = datetime.fromisoformat(body["refresh_expires_at"])
    assert abs((advertised - expires).total_seconds()) < 1


def test_FR_104_the_store_keeps_a_digest_of_the_refresh_token(
    client: TestClient, redis_url: str, sign_in: Callable[..., httpx.Response]
) -> None:
    """The token itself is never written down, only a digest of it."""
    import hashlib

    body = sign_in("+251955555558").json()

    store = SyncRedis.from_url(redis_url)
    stored = [value for key in store.scan_iter(match="session:*") for value in store.hvals(key)]
    store.close()

    assert body["refresh_token"].encode() not in stored
    assert hashlib.sha256(body["refresh_token"].encode()).hexdigest().encode() in stored


def test_FR_104_rotating_hands_back_a_new_pair(
    client: TestClient,
    signing_keys: tuple[str, str],
    sign_in: Callable[..., httpx.Response],
) -> None:
    """Trading a refresh token changes both halves and keeps the same session."""
    body = sign_in("+251955555559").json()

    rotated = client.post(
        "/v1/auth/refresh",
        headers={"authorization": f"Bearer {body['refresh_token']}"},
    )

    assert rotated.status_code == 200
    new_body = rotated.json()
    assert new_body["refresh_token"] != body["refresh_token"]
    assert new_body["access_token"] != body["access_token"]
    assert (
        _decode(new_body["access_token"], signing_keys[1])["sid"]
        == _decode(body["access_token"], signing_keys[1])["sid"]
    )
    assert "user" not in new_body, "rotating must not re-send the account record"


def test_FR_104_signing_out_closes_that_session(
    client: TestClient, sign_in: Callable[..., httpx.Response]
) -> None:
    """After signing out the refresh token is dead."""
    body = sign_in("+251955555560").json()

    signed_out = client.post(
        "/v1/auth/logout",
        headers={"authorization": f"Bearer {body['refresh_token']}"},
    )
    afterwards = client.post(
        "/v1/auth/refresh",
        headers={"authorization": f"Bearer {body['refresh_token']}"},
    )

    assert signed_out.status_code == 204
    assert afterwards.status_code == 401
    assert afterwards.json()["error"]["code"] == "TOKEN_INVALID"
    assert afterwards.json()["error"]["details"]["reason"] == "session_closed"


def test_FR_104_signing_out_with_a_token_nobody_issued_is_not_a_failure(
    client: TestClient,
) -> None:
    """Signing out is something a person does on a device they may no longer control."""
    unreadable = jwt.encode(
        {"sub": "00000000-0000-0000-0000-000000000001", "sid": "x", "kind": "refresh"},
        "not-a-key",
        algorithm="HS256",
    )

    response = client.post("/v1/auth/logout", headers={"authorization": f"Bearer {unreadable}"})

    assert response.status_code == 204


def test_FR_104_a_token_signed_by_somebody_else_buys_nothing(client: TestClient) -> None:
    """A well-formed token with the wrong signature is refused as unreadable."""
    forged = jwt.encode(
        {"sub": "00000000-0000-0000-0000-000000000001", "sid": "x", "kind": "refresh"},
        "not-a-key",
        algorithm="HS256",
    )

    response = client.post("/v1/auth/refresh", headers={"authorization": f"Bearer {forged}"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "TOKEN_INVALID"
    assert response.json()["error"]["details"]["reason"] == "unreadable"


@pytest.mark.parametrize("header", [None, "", "Bearer", "Basic abc123", "bearer"])
def test_FR_104_a_missing_or_malformed_credential_is_refused(
    client: TestClient, header: str | None
) -> None:
    """No header, no scheme, no token: all of them are refused in the same shape."""
    headers = {} if header is None else {"authorization": header}

    response = client.post("/v1/auth/refresh", headers=headers)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "TOKEN_INVALID"


def test_FR_104_an_access_token_cannot_be_traded_for_a_longer_lived_one(
    client: TestClient, sign_in: Callable[..., httpx.Response]
) -> None:
    """Presenting the short half at the refresh door is refused, not upgraded."""
    body = sign_in("+251955555561").json()

    response = client.post(
        "/v1/auth/refresh",
        headers={"authorization": f"Bearer {body['access_token']}"},
    )

    assert response.status_code == 401
    assert response.json()["error"]["details"]["reason"] == "unreadable"


def test_FR_104_a_token_cannot_be_altered(
    client: TestClient, sign_in: Callable[..., httpx.Response]
) -> None:
    """Editing a token invalidates it, because the signature covers the contents."""
    body = sign_in("+251955555562").json()
    original = body["refresh_token"]
    tampered = original[:-3] + ("aaa" if not original.endswith("aaa") else "bbb")

    response = client.post("/v1/auth/refresh", headers={"authorization": f"Bearer {tampered}"})

    assert response.status_code == 401
    assert response.json()["error"]["details"]["reason"] == "unreadable"
