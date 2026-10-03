"""FR-106: one number, one account.

Somebody who reinstalls the app, loses their handset, or simply signs in again must land on
the account they already have, with their history intact. That makes uniqueness a promise
the platform has to keep in the database as well as in the code, because anything else that
can write to the table would otherwise be free to break it.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from redis import Redis as SyncRedis

pytestmark = pytest.mark.integration


def test_FR_106_registering_creates_the_account(
    client: TestClient,
    accounts: Callable[[], list[dict[str, object]]],
    sign_in: Callable[..., httpx.Response],
) -> None:
    """A verified number becomes a row, with the starting values the platform expects."""
    response = sign_in("+251922200001")

    assert response.status_code == 200
    user = response.json()["user"]
    assert user["phone_e164"] == "+251922200001"
    assert user["status"] == "ACTIVE"
    assert user["kyc_tier"] == "TIER_0"

    stored = accounts()
    assert len(stored) == 1
    assert stored[0]["phone_e164"] == "+251922200001"
    assert stored[0]["status"] == "ACTIVE"
    assert stored[0]["kyc_tier"] == "TIER_0"


def test_FR_106_registering_the_same_number_again_signs_the_same_account_in(
    client: TestClient,
    accounts: Callable[[], list[dict[str, object]]],
    sign_in: Callable[..., httpx.Response],
) -> None:
    """Proving the number twice must not produce a second account or lose the first."""
    phone_e164 = "+251922200002"

    first = sign_in(phone_e164)
    second = sign_in(phone_e164)

    assert first.status_code == second.status_code == 200
    assert first.json()["user"]["id"] == second.json()["user"]["id"]
    assert len(accounts()) == 1


def test_FR_106_two_numbers_are_two_accounts(
    client: TestClient,
    accounts: Callable[[], list[dict[str, object]]],
    sign_in: Callable[..., httpx.Response],
) -> None:
    """Two traders do not collide just because they signed up on the same day."""
    first = sign_in("+251922200003")
    second = sign_in("+251922200004")

    assert first.json()["user"]["id"] != second.json()["user"]["id"]
    assert [row["phone_e164"] for row in accounts()] == [
        "+251922200003",
        "+251922200004",
    ]


def test_FR_106_an_account_survives_the_request_that_created_it(
    client: TestClient,
    postgres_url: str,
    sign_in: Callable[..., httpx.Response],
) -> None:
    """The row is committed, not merely visible inside the request that wrote it.

    A write that is flushed and never committed looks correct to the caller and vanishes
    the moment the connection closes, which is the kind of bug that only shows up once
    real traders are relying on it.
    """
    sign_in("+251922200005")

    sync_url = postgres_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(sync_url) as connection:
        rows = connection.execute(
            "SELECT count(*) FROM users WHERE phone_e164 = %s", ("+251922200005",)
        ).fetchone()

    assert rows is not None
    assert rows[0] == 1


def test_FR_106_the_database_refuses_a_duplicate_number(
    postgres_url: str, clean_database: None
) -> None:
    """Uniqueness is enforced by the schema, so no other writer can create a second one."""
    sync_url = postgres_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(sync_url, autocommit=True) as connection:
        for _ in range(2):
            try:
                connection.execute(
                    "INSERT INTO users (id, phone_e164, role, language, kyc_tier, status)"
                    " VALUES (gen_random_uuid(), '+251922200006', 'FARMER', 'am', 'TIER_0',"
                    " 'ACTIVE')"
                )
            except psycopg.errors.UniqueViolation:
                duplicate_refused = True
            else:
                duplicate_refused = False

    assert duplicate_refused


def test_FR_106_the_database_refuses_a_number_that_is_not_ethiopian_mobile(
    postgres_url: str, clean_database: None
) -> None:
    """The format rule is a column-level guarantee, not only an application check.

    The number is the right length and the wrong country, so length is not what stops it.
    """
    sync_url = postgres_url.replace("postgresql+psycopg://", "postgresql://")
    with (
        psycopg.connect(sync_url, autocommit=True) as connection,
        pytest.raises(psycopg.errors.CheckViolation),
    ):
        connection.execute(
            "INSERT INTO users (id, phone_e164, role, language, kyc_tier, status)"
            " VALUES (gen_random_uuid(), '+447700900123', 'FARMER', 'am', 'TIER_0',"
            " 'ACTIVE')"
        )


def test_FR_106_a_closed_account_cannot_be_used_to_reregister(
    client: TestClient,
    redis_url: str,
    postgres_url: str,
    sign_in: Callable[..., httpx.Response],
) -> None:
    """A closed account stays closed: re-proving the number does not reopen it."""
    phone_e164 = "+251922200007"
    sign_in(phone_e164)

    store = SyncRedis.from_url(redis_url)
    sessions_before = set(store.scan_iter(match="session:*"))

    sync_url = postgres_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(sync_url, autocommit=True) as connection:
        connection.execute(
            "UPDATE users SET status = 'CLOSED' WHERE phone_e164 = %s", (phone_e164,)
        )

    again = sign_in(phone_e164)

    assert again.status_code == 403
    assert again.json()["error"]["code"] == "FORBIDDEN"
    assert again.json()["error"]["details"] == {"account_status": "CLOSED"}

    # The session from before the closure is untouched; the refused attempt simply added
    # nothing, which is the whole claim: a closed account gains no new way in.
    assert set(store.scan_iter(match="session:*")) == sessions_before
    store.close()
