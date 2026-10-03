"""AC-1.3: a refresh token that comes back twice closes every door.

A refresh token is the one credential worth stealing, and the only defence is noticing when
one is replayed. Closing just the replayed session would be the wrong answer: whoever holds
it has usually already rotated it, so every session on the account has to go at once.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from fastapi.testclient import TestClient
from redis import Redis as SyncRedis

pytestmark = pytest.mark.integration


def test_AC_1_3_reusing_a_consumed_refresh_token_revokes_every_session(
    client: TestClient, redis_url: str, sign_in: Callable[..., httpx.Response]
) -> None:
    """Two sessions, one replay, no session left open — including the legitimate one."""
    phone_e164 = "+251944400020"
    first_device = sign_in(phone_e164).json()
    second_device = sign_in(phone_e164).json()
    assert first_device["user"]["id"] == second_device["user"]["id"]
    assert first_device["refresh_token"] != second_device["refresh_token"]

    # The legitimate device rotates its token, which consumes the one it presented.
    rotated = client.post(
        "/v1/auth/refresh",
        headers={"authorization": f"Bearer {first_device['refresh_token']}"},
    )
    assert rotated.status_code == 200
    current_token = rotated.json()["refresh_token"]

    # A stolen copy of the original token comes back.
    replayed = client.post(
        "/v1/auth/refresh",
        headers={"authorization": f"Bearer {first_device['refresh_token']}"},
    )

    assert replayed.status_code == 401
    assert replayed.json()["error"]["code"] == "TOKEN_INVALID"
    assert replayed.json()["error"]["details"]["reason"] == "token_reuse"

    store = SyncRedis.from_url(redis_url)
    sessions_left = list(store.scan_iter(match="session:*"))
    account_index = list(store.scan_iter(match="account-sessions:*"))
    store.close()

    assert sessions_left == [], "every session on the account must be closed"
    assert account_index == []

    # The token the honest device was holding dies with the rest, which is the point.
    honest_aftermath = client.post(
        "/v1/auth/refresh",
        headers={"authorization": f"Bearer {current_token}"},
    )
    assert honest_aftermath.status_code == 401

    # The other device's session goes too, even though it was never replayed.
    other_device = client.post(
        "/v1/auth/refresh",
        headers={"authorization": f"Bearer {second_device['refresh_token']}"},
    )
    assert other_device.status_code == 401
    assert other_device.json()["error"]["details"]["reason"] == "session_closed"


def test_AC_1_3_a_replay_closes_the_sessions_of_that_account_only(
    client: TestClient, sign_in: Callable[..., httpx.Response]
) -> None:
    """Revocation has to be thorough and targeted: one account's replay must not sign
    every other trader out of their own session."""
    victim = sign_in("+251944400023").json()
    bystander = sign_in("+251944400024").json()

    client.post(
        "/v1/auth/refresh",
        headers={"authorization": f"Bearer {victim['refresh_token']}"},
    )
    replayed = client.post(
        "/v1/auth/refresh",
        headers={"authorization": f"Bearer {victim['refresh_token']}"},
    )

    assert replayed.status_code == 401
    assert replayed.json()["error"]["details"]["reason"] == "token_reuse"

    unaffected = client.post(
        "/v1/auth/refresh",
        headers={"authorization": f"Bearer {bystander['refresh_token']}"},
    )

    assert unaffected.status_code == 200
    assert unaffected.json()["refresh_token"] != bystander["refresh_token"]
