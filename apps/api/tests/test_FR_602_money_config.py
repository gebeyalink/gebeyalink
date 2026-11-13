"""FR-602: the money module reads the active ``fee_config`` row, not process configuration.

The arithmetic is checked without a database in ``test_FR_602_money_math.py``. This file checks
the other half of the requirement: which numbers reach that arithmetic. A policy change must
move a future order's fee without a redeploy, and a deployment with no active row must refuse to
price the order rather than quietly use the constants the code happened to ship with.

The tests arrange the table directly where the question is the reader's, and through the API
where the question is the order's. The maker/checker route that writes the row is exercised in
``test_SEC_13_maker_checker.py``; here the row is the input.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration

#: The published, seeded policy. The same five values the migration writes.
SEED_POLICY = (200, 500, 50_000, 1_500)

#: A small, cheap listing so the percentage is what the test is looking at, not the ceiling.
LISTING_KG = 1_000
BUY_KG = 100
UNIT_PRICE_SANTIM = 10_000
SUBTOTAL_SANTIM = BUY_KG * UNIT_PRICE_SANTIM


def _restore_seeded_policy(postgres_url: str) -> None:
    """Put the table back to the single row migration 0019 leaves behind."""
    sync_url = postgres_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(sync_url, autocommit=True) as connection:
        connection.execute("TRUNCATE TABLE fee_config")
        connection.execute("""
            INSERT INTO fee_config (fee_payer, platform_fee_bps, min_fee_santim,
                                    max_fee_santim, vat_bps, effective_from)
            VALUES ('BUYER', 200, 500, 50000, 1500, now())
            """)


@pytest.fixture(autouse=True)
def seeded_policy(postgres_url: str) -> Iterator[None]:
    """Restore the published policy around every test.

    ``fee_config`` has no foreign key from any other table, so truncating it is safe and leaves
    the rest of the fixture graph alone. Each test then starts from the one row a fresh deploy
    would have, and the next test cannot inherit this one's experiment.
    """
    _restore_seeded_policy(postgres_url)
    yield
    _restore_seeded_policy(postgres_url)


def _set_policy(
    postgres_url: str,
    *,
    fee_payer: str = "BUYER",
    platform_fee_bps: int,
    min_fee_santim: int,
    max_fee_santim: int,
    vat_bps: int,
) -> None:
    """Change the active row directly, as if an approved editor change had landed."""
    sync_url = postgres_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(sync_url, autocommit=True) as connection:
        connection.execute(
            "UPDATE fee_config SET fee_payer = %s, platform_fee_bps = %s,"
            " min_fee_santim = %s, max_fee_santim = %s, vat_bps = %s"
            " WHERE effective_to IS NULL",
            (fee_payer, platform_fee_bps, min_fee_santim, max_fee_santim, vat_bps),
        )


def _post_listing(
    client: TestClient,
    headers: dict[str, str],
    listing_body: Callable[..., dict[str, object]],
) -> str:
    response = client.post(
        "/v1/listings",
        headers=headers,
        json=listing_body(
            quantity_kg=LISTING_KG,
            min_order_kg=50,
            unit_price_santim=UNIT_PRICE_SANTIM,
        ),
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def _buy(
    client: TestClient,
    headers: dict[str, str],
    listing_id: str,
    order_body: Callable[..., dict[str, object]],
    key: Callable[[], str],
    *,
    quantity_kg: int = BUY_KG,
) -> httpx.Response:
    response = client.post(
        f"/v1/listings/{listing_id}/buy-now",
        headers={**headers, "Idempotency-Key": key()},
        json=order_body(quantity_kg=quantity_kg),
    )
    assert isinstance(response, httpx.Response)
    return response


def test_the_seeded_policy_is_the_published_one(postgres_url: str) -> None:
    """The migration seeds buyer-funded, 2%, 5 birr floor, 500 birr ceiling, 15% VAT."""
    sync_url = postgres_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(sync_url) as connection:
        row = connection.execute(
            "SELECT fee_payer, platform_fee_bps, min_fee_santim, max_fee_santim, vat_bps,"
            " effective_from, effective_to FROM fee_config"
        ).fetchone()

    assert row is not None
    assert row[:5] == ("BUYER", *SEED_POLICY)
    effective_from = row[5]
    assert effective_from <= datetime.now(UTC)
    assert row[6] is None


def test_a_future_order_uses_the_active_row(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    postgres_url: str,
    stored_orders: Callable[[], list[dict[str, object]]],
) -> None:
    """All four configured numbers reach the order: percentage, bounds and VAT."""
    _set_policy(
        postgres_url,
        platform_fee_bps=500,
        min_fee_santim=1_000,
        max_fee_santim=100_000,
        vat_bps=500,
    )
    seller = signed_in()
    listing_id = _post_listing(client, seller, listing_body)
    buyer = signed_in("+251911000003")

    response = _buy(client, buyer, listing_id, order_body, idempotency_key)

    assert response.status_code == 201, response.text
    order = response.json()
    # 5% of 1,000,000 is 50,000; 5% VAT on that is 2,500.
    assert order["platform_fee_santim"] == 50_000
    assert order["vat_santim"] == 2_500
    assert order["total_santim"] == SUBTOTAL_SANTIM + 52_500

    stored = stored_orders()[0]
    assert stored["platform_fee_santim"] == 50_000
    assert stored["vat_santim"] == 2_500
    assert stored["total_santim"] == SUBTOTAL_SANTIM + 52_500


def test_each_order_reads_the_policy_in_force_at_its_own_moment(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    postgres_url: str,
) -> None:
    """A service built before the change must not price the next order under the old policy."""
    seller = signed_in()
    listing_id = _post_listing(client, seller, listing_body)
    first_buyer = signed_in("+251911000003")

    first = _buy(client, first_buyer, listing_id, order_body, idempotency_key)
    assert first.status_code == 201, first.text
    assert first.json()["platform_fee_santim"] == 20_000
    assert first.json()["vat_santim"] == 3_000

    _set_policy(
        postgres_url,
        platform_fee_bps=1_000,
        min_fee_santim=0,
        max_fee_santim=200_000,
        vat_bps=0,
    )
    second_buyer = signed_in("+251911000004")
    second = _buy(client, second_buyer, listing_id, order_body, idempotency_key)

    assert second.status_code == 201, second.text
    assert second.json()["platform_fee_santim"] == 100_000
    assert second.json()["vat_santim"] == 0
    assert second.json()["total_santim"] == SUBTOTAL_SANTIM + 100_000


def test_a_missing_active_policy_is_an_internal_error_not_a_fallback(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    postgres_url: str,
    stored_orders: Callable[[], list[dict[str, object]]],
) -> None:
    """With no row in force, the order service refuses rather than charging a stale constant."""
    seller = signed_in()
    listing_id = _post_listing(client, seller, listing_body)
    buyer = signed_in("+251911000003")
    sync_url = postgres_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(sync_url, autocommit=True) as connection:
        connection.execute("DELETE FROM fee_config")

    response = _buy(client, buyer, listing_id, order_body, idempotency_key)

    assert response.status_code == 500, response.text
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert response.json()["error"]["details"] == {"reason": "fee_config_missing"}
    assert stored_orders() == []
