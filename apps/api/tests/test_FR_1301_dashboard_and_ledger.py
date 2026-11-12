"""FR-1301: the dashboard's honest numbers, the order ledger, and the price override.

The dashboard is an instrument, and an instrument that reports zero because it cannot see is
worse than one that says it cannot see. These tests therefore check both the arithmetic the COD
pilot can do and the nulls it must not turn into zeroes. The ledger is read-only and, for a COD
order, empty by construction. The price override is the one mutation here, and its proof is the
audit row: one override, one audit entry, and the reason inside the after-state hash.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast

import httpx
import psycopg
import psycopg.rows
import pytest
from fastapi.testclient import TestClient

from apps.api.core import staff_totp
from apps.api.repositories.audit_log_repository import state_hash

pytestmark = [pytest.mark.integration]

REPO_ROOT = Path(__file__).resolve().parents[3]
GATEWAY_SECRET = "test-gateway-secret"
TERMS_VERSION = "2026-01-01"
TOTP_ENCRYPTION_KEY = "ZGVmZ2hpamtsbW5vcHFyc3R1dnd4eXphYmNkZWZnaGk="

ADMIN_EMAIL = "admin-dashboard@gebeyalink.example"
SUPPORT_EMAIL = "support-dashboard@gebeyalink.example"
STAFF_PASSWORD = "a-good-long-staff-password"

SELLER_PHONE = "+251911300001"
BUYER_PHONE = "+251911300002"
STRANGER_PHONE = "+251911300003"

CHEAP_ASK_SANTIM = 1_000
COD_KG = 200
COD_SUBTOTAL_SANTIM = CHEAP_ASK_SANTIM * COD_KG

Rows = list[dict[str, object]]
Completed = subprocess.CompletedProcess[str]


# --------------------------------------------------------------------------- fixtures


@pytest.fixture()
def sql(postgres_url: str) -> Iterator[Callable[[str, tuple[object, ...] | None], Rows]]:
    """Run one statement straight against PostgreSQL and read any rows back."""
    sync_url = postgres_url.replace("postgresql+psycopg://", "postgresql://")

    def _run(statement: str, params: tuple[object, ...] | None = None) -> Rows:
        with (
            psycopg.connect(sync_url, autocommit=True) as connection,
            connection.cursor(row_factory=psycopg.rows.dict_row) as cursor,
        ):
            cursor.execute(statement, params)
            if cursor.description is None:
                return []
            return list(cursor.fetchall())

    yield _run


@pytest.fixture(autouse=True)
def clean_staff_tables(postgres_url: str) -> Iterator[None]:
    """Empty the staff and audit tables before each test.

    ``TRUNCATE`` is not caught by the append-only trigger, which is why it is what a test may
    use and a runtime process may not.
    """
    sync_url = postgres_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(sync_url, autocommit=True) as connection:
        connection.execute("TRUNCATE TABLE audit_log")
        connection.execute("TRUNCATE TABLE staff_sessions")
        connection.execute("TRUNCATE TABLE staff_users CASCADE")
    yield


@pytest.fixture()
def run_cli(postgres_url: str, redis_url: str) -> Callable[..., Completed]:
    """Run an operator command in its own process, as an operator would."""
    environment = {
        **os.environ,
        "DATABASE_URL": postgres_url,
        "REDIS_URL": redis_url,
        "SIGNING_PRIVATE_KEY": "unused-outside-the-application",
        "SIGNING_PUBLIC_KEY": "unused-outside-the-application",
        "GATEWAY_SECRET": GATEWAY_SECRET,
        "TERMS_VERSION": TERMS_VERSION,
        "TOTP_ENCRYPTION_KEY": TOTP_ENCRYPTION_KEY,
    }
    environment.pop("PYTHONIOENCODING", None)

    def _run(module: str, *args: str, stdin: str | None = None) -> Completed:
        return subprocess.run(
            [sys.executable, "-m", module, *args],
            cwd=REPO_ROOT,
            env=environment,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

    return _run


def _seed_staff(
    run_cli: Callable[..., Completed],
    *,
    email: str,
    role: str,
    password: str = STAFF_PASSWORD,
) -> Completed:
    return run_cli(
        "apps.api.management.seed_staff",
        "--email",
        email,
        "--role",
        role,
        stdin=password + "\n",
    )


def _uri_from_stdout(stdout: str) -> str:
    return next(line for line in stdout.splitlines() if line.startswith("otpauth://"))


def _secret_from_uri(uri: str) -> bytes:
    import base64
    from urllib.parse import parse_qs, urlparse

    query = parse_qs(urlparse(uri).query)
    secret_b32 = query["secret"][0]
    padding = "=" * (-len(secret_b32) % 8)
    return base64.b32decode(secret_b32 + padding)


def _code(secret: bytes, *, at: datetime | None = None) -> str:
    return staff_totp.current_code(secret, at=at)


def _login(
    client: TestClient,
    email: str,
    secret: bytes,
    *,
    password: str = STAFF_PASSWORD,
) -> httpx.Response:
    return cast(
        httpx.Response,
        client.post(
            "/v1/admin/login",
            json={
                "email": email,
                "password": password,
                "totp_code": _code(secret),
            },
        ),
    )


def _full_token(client: TestClient, email: str, secret: bytes, password: str) -> str:
    first = _login(client, email, secret, password=password)
    assert first.status_code == 200, first.text
    assert first.json()["scope"] == "change_password_only"
    changed = client.post(
        "/v1/admin/change-password",
        json={
            "old_password": password,
            "new_password": "the-new-long-staff-password",
            "totp_code": _code(secret),
        },
        headers={"Authorization": f"Bearer {first.json()['access_token']}"},
    )
    assert changed.status_code == 204, changed.text
    second = _login(client, email, secret, password="the-new-long-staff-password")
    assert second.status_code == 200, second.text
    assert second.json()["scope"] == "full"
    return cast(str, second.json()["access_token"])


def _staff_headers(
    client: TestClient,
    run_cli: Callable[..., Completed],
    *,
    email: str,
    role: str,
) -> dict[str, str]:
    seeded = _seed_staff(run_cli, email=email, role=role)
    assert seeded.returncode == 0, seeded.stdout + seeded.stderr
    secret = _secret_from_uri(_uri_from_stdout(seeded.stdout))
    token = _full_token(client, email, secret, STAFF_PASSWORD)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def admin_headers(cod_client: TestClient, run_cli: Callable[..., Completed]) -> dict[str, str]:
    return _staff_headers(cod_client, run_cli, email=ADMIN_EMAIL, role="ADMIN")


@pytest.fixture()
def support_headers(cod_client: TestClient, run_cli: Callable[..., Completed]) -> dict[str, str]:
    return _staff_headers(cod_client, run_cli, email=SUPPORT_EMAIL, role="SUPPORT")


# --------------------------------------------------------------------------- helpers


def _post_listing(
    client: TestClient,
    headers: dict[str, str],
    listing_body: Callable[..., dict[str, object]],
    **overrides: object,
) -> str:
    response = client.post(
        "/v1/listings",
        headers=headers,
        json=listing_body(
            quantity_kg=2_000,
            unit_price_santim=CHEAP_ASK_SANTIM,
            **overrides,
        ),
    )
    assert response.status_code == 201, response.text
    return cast(str, response.json()["id"])


def _buy_cod_order(
    client: TestClient,
    headers: dict[str, str],
    listing_id: str,
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
) -> dict[str, object]:
    response = client.post(
        f"/v1/listings/{listing_id}/buy-now",
        headers={**headers, "Idempotency-Key": idempotency_key()},
        json=order_body(payment_mode="COD", quantity_kg=COD_KG),
    )
    assert response.status_code == 201, response.text
    return cast(dict[str, object], response.json())


def _complete_order(sql: Callable[..., Rows], order_id: object) -> None:
    sql(
        "UPDATE orders SET status = 'COMPLETED', delivered_at = now() WHERE id = %s",
        (str(order_id),),
    )


def _set_listing_source(sql: Callable[..., Rows], listing_id: str, source: str) -> None:
    sql("UPDATE listings SET source = %s WHERE id = %s", (source, listing_id))


def _insert_accepted_offer(
    sql: Callable[..., Rows],
    *,
    listing_id: str,
    buyer_id: str,
) -> None:
    sql(
        "INSERT INTO offers"
        " (id, listing_id, buyer_id, thread_id, thread_depth, quantity_kg, price_santim,"
        "  valid_until, status, payment_mode)"
        " VALUES"
        " (%s, %s, %s, %s, 0, %s, %s, now() + interval '1 day', 'ACCEPTED', 'COD')",
        (str(uuid.uuid4()), listing_id, buyer_id, str(uuid.uuid4()), 100, CHEAP_ASK_SANTIM),
    )


def _user_id(sql: Callable[..., Rows], phone_e164: str) -> str:
    rows = sql("SELECT id FROM users WHERE phone_e164 = %s", (phone_e164,))
    assert rows, phone_e164
    return str(rows[0]["id"])


def _setup_completed_cod_order(
    client: TestClient,
    seller_headers: dict[str, str],
    buyer_headers: dict[str, str],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    sql: Callable[..., Rows],
) -> tuple[str, str, str]:
    listing_id = _post_listing(client, seller_headers, listing_body)
    order = _buy_cod_order(client, buyer_headers, listing_id, order_body, idempotency_key)
    _complete_order(sql, order["id"])
    return listing_id, str(order["id"]), _user_id(sql, BUYER_PHONE)


# ------------------------------------------------------------------- the dashboard


def test_dashboard_returns_every_metric_and_zero_gmv_during_the_cod_pilot(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    cod_signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    admin_headers: dict[str, str],
    sql: Callable[..., Rows],
) -> None:
    """All seven metrics are present; GMV is a real zero while COD GMV is the real figure."""
    seller = cod_signed_in(SELLER_PHONE)
    buyer = cod_signed_in_as_buyer(BUYER_PHONE)
    _setup_completed_cod_order(
        cod_client, seller, buyer, listing_body, order_body, idempotency_key, sql
    )

    response = cod_client.get("/v1/admin/dashboard", headers=admin_headers)
    assert response.status_code == 200, response.text
    body = response.json()

    assert {
        "gmv_santim",
        "cod_gmv_santim",
        "completed_orders",
        "active_farmers",
        "offer_to_order_conversion",
        "offer_to_order_conversion_note",
        "cod_offer_to_order_conversion",
        "dispute_rate",
        "dispute_rate_note",
        "nps",
        "nps_surveyed_at",
        "nps_note",
        "ussd_share",
        "as_of",
    } <= set(body)
    assert body["gmv_santim"] == 0
    assert body["cod_gmv_santim"] == COD_SUBTOTAL_SANTIM
    assert body["completed_orders"] == 1
    assert body["active_farmers"] == 1
    assert body["offer_to_order_conversion"] is None
    assert body["offer_to_order_conversion_note"]
    assert body["dispute_rate"] is None
    assert body["dispute_rate_note"]
    assert body["nps"] is None
    assert body["nps_surveyed_at"] is None
    assert body["nps_note"]
    assert body["ussd_share"] == 0.0
    assert datetime.fromisoformat(body["as_of"])


def test_offer_to_order_conversion_is_null_with_a_note_and_has_a_cod_equivalent(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    cod_signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    admin_headers: dict[str, str],
    sql: Callable[..., Rows],
) -> None:
    """The funded conversion is null; the COD equivalent is a number when it has a denominator."""
    seller = cod_signed_in(SELLER_PHONE)
    buyer = cod_signed_in_as_buyer(BUYER_PHONE)
    listing_id, _order_id, buyer_id = _setup_completed_cod_order(
        cod_client, seller, buyer, listing_body, order_body, idempotency_key, sql
    )
    _insert_accepted_offer(sql, listing_id=listing_id, buyer_id=buyer_id)

    response = cod_client.get("/v1/admin/dashboard", headers=admin_headers)
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["offer_to_order_conversion"] is None
    assert "COD" in body["offer_to_order_conversion_note"]
    assert body["cod_offer_to_order_conversion"] == 1.0


def test_dispute_rate_and_nps_are_null_with_reasons_when_not_measured(
    cod_client: TestClient,
    admin_headers: dict[str, str],
) -> None:
    """A metric nobody has measured is null and says why; it is never dressed up as zero."""
    response = cod_client.get("/v1/admin/dashboard", headers=admin_headers)
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["dispute_rate"] is None
    assert "Phase 2" in body["dispute_rate_note"]
    assert body["nps"] is None
    assert body["nps_surveyed_at"] is None
    assert body["nps_note"] == "NPS has not been surveyed yet."


def test_ussd_share_is_the_fraction_of_ussd_and_sms_listings(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    admin_headers: dict[str, str],
    sql: Callable[..., Rows],
) -> None:
    """Two listings, one from USSD, give a real half rather than a rounded story."""
    seller = cod_signed_in(SELLER_PHONE)
    ussd_listing = _post_listing(cod_client, seller, listing_body)
    _post_listing(cod_client, seller, listing_body)
    _set_listing_source(sql, ussd_listing, "USSD")

    response = cod_client.get("/v1/admin/dashboard", headers=admin_headers)
    assert response.status_code == 200, response.text
    assert response.json()["ussd_share"] == 0.5


# ---------------------------------------------------------------------- the ledger


def test_a_cod_order_ledger_is_empty_with_the_exact_note(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    cod_signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    admin_headers: dict[str, str],
    sql: Callable[..., Rows],
) -> None:
    """COD never creates journals, and the response says that rather than looking broken."""
    seller = cod_signed_in(SELLER_PHONE)
    buyer = cod_signed_in_as_buyer(BUYER_PHONE)
    listing_id = _post_listing(cod_client, seller, listing_body)
    order = _buy_cod_order(cod_client, buyer, listing_id, order_body, idempotency_key)

    response = cod_client.get(f"/v1/admin/orders/{order['id']}/ledger", headers=admin_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["entries"] == []
    assert body["note"] == "no ledger entries — COD orders never create journals"


def test_staff_may_read_any_order_ledger_but_a_trader_may_not(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    cod_signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    support_headers: dict[str, str],
    sql: Callable[..., Rows],
) -> None:
    """Support reads an order it is not a party to; a trader gets the door."""
    seller = cod_signed_in(SELLER_PHONE)
    buyer = cod_signed_in_as_buyer(BUYER_PHONE)
    listing_id = _post_listing(cod_client, seller, listing_body)
    order = _buy_cod_order(cod_client, buyer, listing_id, order_body, idempotency_key)
    stranger = cod_signed_in(STRANGER_PHONE)

    refused = cod_client.get(f"/v1/admin/orders/{order['id']}/ledger", headers=stranger)
    assert refused.status_code == 403, refused.text

    allowed = cod_client.get(f"/v1/admin/orders/{order['id']}/ledger", headers=support_headers)
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["order_id"] == order["id"]


# ------------------------------------------------------------------ the price board


def _override_url(day: date) -> str:
    return f"/v1/admin/prices/1/OR/{day.isoformat()}"


def test_price_override_writes_the_board_row_and_one_audit_with_the_reason(
    cod_client: TestClient,
    admin_headers: dict[str, str],
    sql: Callable[..., Rows],
) -> None:
    """One override, one board row, one audit row, and the reason inside the after-hash."""
    day = datetime.now(UTC).date()
    sql(
        "INSERT INTO price_indices"
        " (crop_id, region_code, day, avg_unit_price_santim, sample_size, insufficient_data)"
        " VALUES (1, 'OR', %s, 10000, 40, false)",
        (day,),
    )
    reason = "manual correction after a bad batch"

    response = cod_client.put(
        _override_url(day),
        headers=admin_headers,
        json={"price_santim": 12_345, "reason": f"  {reason}  "},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "crop_id": 1,
        "region_code": "OR",
        "day": day.isoformat(),
        "price_santim": 12_345,
    }

    boards = sql(
        "SELECT id, avg_unit_price_santim, min_unit_price_santim, max_unit_price_santim,"
        " sample_size, insufficient_data FROM price_indices"
        " WHERE crop_id = 1 AND region_code = 'OR' AND day = %s",
        (day,),
    )
    assert len(boards) == 1
    board = boards[0]
    assert board["avg_unit_price_santim"] == 12_345
    assert board["min_unit_price_santim"] == 12_345
    assert board["max_unit_price_santim"] == 12_345
    assert board["sample_size"] == 0
    assert board["insufficient_data"] is False

    audits = sql(
        "SELECT target_type, target_id, before_hash, after_hash FROM audit_log"
        " WHERE action = 'PRICE_BOARD_OVERRIDDEN'",
    )
    assert len(audits) == 1
    audit = audits[0]
    assert audit["target_type"] == "price_index"
    assert str(audit["target_id"]) == str(board["id"])
    assert audit["before_hash"] == state_hash({"price_santim": 10_000})
    assert audit["after_hash"] == state_hash({"price_santim": 12_345, "reason": reason})


def test_support_may_not_override_the_price_board(
    cod_client: TestClient,
    support_headers: dict[str, str],
    sql: Callable[..., Rows],
) -> None:
    """The override is an ADMIN action; support can read the board but not rewrite it."""
    day = datetime.now(UTC).date()
    response = cod_client.put(
        _override_url(day),
        headers=support_headers,
        json={"price_santim": 12_345, "reason": "support cannot do this"},
    )
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "FORBIDDEN"
    assert sql("SELECT id FROM price_indices") == []
    assert sql("SELECT id FROM audit_log WHERE action = 'PRICE_BOARD_OVERRIDDEN'") == []


def test_a_non_positive_override_price_is_a_validation_error(
    cod_client: TestClient,
    admin_headers: dict[str, str],
    sql: Callable[..., Rows],
) -> None:
    """A zero price is not an override; it is a validation failure before any write."""
    day = datetime.now(UTC).date()
    response = cod_client.put(
        _override_url(day),
        headers=admin_headers,
        json={"price_santim": 0, "reason": "zero is not a price"},
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert sql("SELECT id FROM price_indices") == []
    assert sql("SELECT id FROM audit_log WHERE action = 'PRICE_BOARD_OVERRIDDEN'") == []
