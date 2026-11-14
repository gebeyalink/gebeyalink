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

import uuid
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient

from apps.api.db.session import get_session_factory
from apps.api.repositories.audit_log_repository import AuditLogRepository
from apps.api.repositories.fee_config_repository import FeeConfigRepository
from apps.api.repositories.pending_change_repository import PendingChangeRepository
from apps.api.repositories.staff_user_repository import StaffUserRepository
from apps.api.services.maker_checker import MakerCheckerService
from packages.shared.money import FeePayer

pytestmark = pytest.mark.integration

#: The published, seeded policy. The same five values the migration writes.
SEED_POLICY = (200, 500, 50_000, 1_500)

#: A small, cheap listing so the percentage is what the test is looking at, not the ceiling.
LISTING_KG = 1_000
BUY_KG = 100
UNIT_PRICE_SANTIM = 10_000
SUBTOTAL_SANTIM = BUY_KG * UNIT_PRICE_SANTIM

#: Two admin rows for the service-level maker/checker step in the stored-totals test. The
#: credentials are never used: this test is about the fee applier, not about staff sign-in.
FEE_MAKER_EMAIL = "fr602-maker@gebeyalink.example"
FEE_CHECKER_EMAIL = "fr602-checker@gebeyalink.example"


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


def _ensure_fee_staff(postgres_url: str) -> None:
    """Create the two admin rows the maker/checker step needs, without a sign-in handshake."""
    sync_url = postgres_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(sync_url, autocommit=True) as connection:
        for email in (FEE_MAKER_EMAIL, FEE_CHECKER_EMAIL):
            connection.execute(
                """
                INSERT INTO staff_users (
                    id, email, password_hash, totp_secret_encrypted, role,
                    must_change_password
                ) VALUES (%s, %s, 'unused-in-this-test', 'unused-in-this-test',
                          'ADMIN', false)
                ON CONFLICT (email) DO UPDATE
                SET role = 'ADMIN', disabled_at = NULL
                """,
                (uuid.uuid4(), email),
            )


async def _submit_and_approve_fee_change(
    *,
    platform_fee_bps: int,
    vat_bps: int,
) -> None:
    """Run the real maker/checker service, rather than editing the active row directly."""
    async with get_session_factory()() as session:
        maker = await StaffUserRepository(session).find_by_email(FEE_MAKER_EMAIL)
        assert maker is not None
        service = MakerCheckerService(
            pending=PendingChangeRepository(session),
            fee_configs=FeeConfigRepository(session),
            audit=AuditLogRepository(session),
        )
        change = await service.submit_fee_change(
            actor=maker,
            fee_payer=FeePayer.BUYER,
            platform_fee_bps=platform_fee_bps,
            min_fee_santim=500,
            max_fee_santim=50_000,
            vat_bps=vat_bps,
            reason="Exercise the stored-totals invariant.",
        )
        change_id = change.id

    async with get_session_factory()() as session:
        checker = await StaffUserRepository(session).find_by_email(FEE_CHECKER_EMAIL)
        assert checker is not None
        service = MakerCheckerService(
            pending=PendingChangeRepository(session),
            fee_configs=FeeConfigRepository(session),
            audit=AuditLogRepository(session),
        )
        await service.approve(change_id=change_id, actor=checker)


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


async def test_a_fee_change_does_not_reprice_an_existing_order(
    concurrent_client: httpx.AsyncClient,
    signed_in_concurrently: Callable[..., Awaitable[dict[str, str]]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    postgres_url: str,
    stored_orders: Callable[[], list[dict[str, object]]],
) -> None:
    """An order keeps the price it was created at; the next order gets the new policy.

    The order is read again *after* the policy has moved, not only before it. That is the
    difference between showing that a total was stored and showing that the stored total is what
    the buyer continues to owe when the platform changes its fee.
    """
    _ensure_fee_staff(postgres_url)
    seller = await signed_in_concurrently()
    listing = await concurrent_client.post(
        "/v1/listings",
        headers=seller,
        json=listing_body(
            quantity_kg=LISTING_KG,
            min_order_kg=50,
            unit_price_santim=UNIT_PRICE_SANTIM,
        ),
    )
    assert listing.status_code == 201, listing.text
    listing_id = str(listing.json()["id"])

    first_buyer = await signed_in_concurrently("+251911000003")
    first = await concurrent_client.post(
        f"/v1/listings/{listing_id}/buy-now",
        headers={**first_buyer, "Idempotency-Key": idempotency_key()},
        json=order_body(quantity_kg=BUY_KG),
    )
    assert first.status_code == 201, first.text
    first_id = str(first.json()["id"])

    before = next(row for row in stored_orders() if str(row["id"]) == first_id)
    # The seeded policy is 2%, so 20,000 santim plus 15% VAT of 3,000.
    assert before["platform_fee_santim"] == 20_000
    assert before["vat_santim"] == 3_000
    assert before["total_santim"] == SUBTOTAL_SANTIM + 23_000
    recorded = {
        "platform_fee_santim": before["platform_fee_santim"],
        "vat_santim": before["vat_santim"],
        "total_santim": before["total_santim"],
        # The buyer's total is stored as total_santim; there is no separate column.
        "buyer_total_santim": before["total_santim"],
    }

    await _submit_and_approve_fee_change(platform_fee_bps=300, vat_bps=1_500)

    after = next(row for row in stored_orders() if str(row["id"]) == first_id)
    assert {
        "platform_fee_santim": after["platform_fee_santim"],
        "vat_santim": after["vat_santim"],
        "total_santim": after["total_santim"],
        "buyer_total_santim": after["total_santim"],
    } == recorded

    second_buyer = await signed_in_concurrently("+251911000004")
    second = await concurrent_client.post(
        f"/v1/listings/{listing_id}/buy-now",
        headers={**second_buyer, "Idempotency-Key": idempotency_key()},
        json=order_body(quantity_kg=BUY_KG),
    )
    assert second.status_code == 201, second.text
    # The approved 3% policy: 30,000 santim plus 15% VAT of 4,500.
    assert second.json()["platform_fee_santim"] == 30_000
    assert second.json()["vat_santim"] == 4_500
    assert second.json()["total_santim"] == SUBTOTAL_SANTIM + 34_500
