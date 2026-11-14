"""SEC-13: a financial change needs two staff accounts, and the audit says both happened.

The queue is generic, but this file drives the one applier this slice wires to it: a change to
the fee policy. The assertions read PostgreSQL directly where the point is that a row exists,
and through the API where the point is what a caller sees. The two together are the requirement:
the route must refuse a self-approval, and the rows must show that nothing was applied anyway.

The fee policy is reset before each test rather than left as the next test found it. The fixture
also truncates the audit log with ``TRUNCATE``, because its append-only trigger refuses a
``DELETE`` by design; a test is allowed to start from a clean book, a running platform is not.
"""

from __future__ import annotations

import asyncio
import base64
import os
import subprocess
import sys
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, urlparse

import httpx
import psycopg
import psycopg.errors
import psycopg.rows
import pytest
from fastapi.testclient import TestClient

from apps.api.core import staff_totp
from apps.api.db.session import get_session_factory
from apps.api.models.pending_change import PendingChange
from apps.api.models.staff_user import StaffRole, StaffUser
from apps.api.repositories.audit_log_repository import AuditLogRepository
from apps.api.repositories.fee_config_repository import FeeConfigRepository
from apps.api.repositories.pending_change_repository import PendingChangeRepository
from apps.api.repositories.staff_user_repository import StaffUserRepository
from apps.api.services.maker_checker import MakerCheckerService
from packages.shared.errors import ApiError, ErrorCode

pytestmark = [pytest.mark.integration]

REPO_ROOT = Path(__file__).resolve().parents[3]
GATEWAY_SECRET = "test-gateway-secret"
TERMS_VERSION = "2026-01-01"
TOTP_ENCRYPTION_KEY = "ZGVmZ2hpamtsbW5vcHFyc3R1dnd4eXphYmNkZWZnaGk="

MAKER_EMAIL = "maker-sec13@gebeyalink.example"
CHECKER_EMAIL = "checker-sec13@gebeyalink.example"
APPROVER_B_EMAIL = "approver-b-sec13@gebeyalink.example"
APPROVER_C_EMAIL = "approver-c-sec13@gebeyalink.example"
SUPPORT_EMAIL = "support-sec13@gebeyalink.example"
STAFF_PASSWORD = "a-good-long-staff-password"

ACTION_SUBMITTED = "FEE_CONFIG_CHANGE_SUBMITTED"
ACTION_APPROVED = "FEE_CONFIG_CHANGE_APPROVED"
ACTION_REJECTED = "FEE_CONFIG_CHANGE_REJECTED"

Rows = list[dict[str, object]]
Completed = subprocess.CompletedProcess[str]

#: The policy migration 0019 seeds. Each test starts from this exact row.
SEED_POLICY = {
    "fee_payer": "BUYER",
    "platform_fee_bps": 200,
    "min_fee_santim": 500,
    "max_fee_santim": 50_000,
    "vat_bps": 1_500,
}


# --------------------------------------------------------------------------- fixtures


@pytest.fixture()
def sql(postgres_url: str) -> Callable[[str, tuple[object, ...] | None], Rows]:
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

    return _run


def _run_cli(postgres_url: str, redis_url: str, *args: str, stdin: str) -> Completed:
    """Run one operator command in its own process, as an operator would."""
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
    return subprocess.run(
        [sys.executable, "-m", "apps.api.management.seed_staff", *args],
        cwd=REPO_ROOT,
        env=environment,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _secret_from_uri(uri: str) -> bytes:
    """The raw TOTP secret out of a provisioning URI, re-encoded from base32."""
    query = parse_qs(urlparse(uri).query)
    secret_b32 = query["secret"][0]
    padding = "=" * (-len(secret_b32) % 8)
    return base64.b32decode(secret_b32 + padding)


@pytest.fixture(scope="module")
def staff_secrets(postgres_url: str, redis_url: str) -> dict[str, bytes]:
    """The staff accounts the tests need, with known TOTP secrets.

    The accounts are seeded once for the module because each seed is a subprocess and the
    per-test fixture deliberately keeps the staff rows. ``must_change_password`` is cleared
    directly: every test wants a full-scoped token, and going through the change-password
    handshake once per test would test the handshake rather than the fee queue.
    """
    sync_url = postgres_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(sync_url, autocommit=True) as connection:
        connection.execute("TRUNCATE TABLE audit_log")
        connection.execute("TRUNCATE TABLE staff_sessions")
        connection.execute("TRUNCATE TABLE staff_users CASCADE")
        connection.execute("TRUNCATE TABLE fee_config")
        connection.execute("""
            INSERT INTO fee_config (fee_payer, platform_fee_bps, min_fee_santim,
                                    max_fee_santim, vat_bps, effective_from)
            VALUES ('BUYER', 200, 500, 50000, 1500, now())
            """)

    secrets: dict[str, bytes] = {}
    for email, role in (
        (MAKER_EMAIL, "ADMIN"),
        (CHECKER_EMAIL, "ADMIN"),
        (APPROVER_B_EMAIL, "ADMIN"),
        (APPROVER_C_EMAIL, "ADMIN"),
        (SUPPORT_EMAIL, "SUPPORT"),
    ):
        result = _run_cli(
            postgres_url,
            redis_url,
            "--email",
            email,
            "--role",
            role,
            stdin=STAFF_PASSWORD + "\n",
        )
        assert result.returncode == 0, result.stdout + result.stderr
        uri = next(line for line in result.stdout.splitlines() if line.startswith("otpauth://"))
        secrets[email] = _secret_from_uri(uri)

    with psycopg.connect(sync_url, autocommit=True) as connection:
        connection.execute("UPDATE staff_users SET must_change_password = false")
    return secrets


def _reset_review_state(postgres_url: str) -> None:
    """Empty the review book and put the published policy back in force."""
    sync_url = postgres_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(sync_url, autocommit=True) as connection:
        connection.execute("TRUNCATE TABLE audit_log")
        connection.execute("TRUNCATE TABLE pending_changes")
        connection.execute("TRUNCATE TABLE staff_sessions")
        connection.execute("TRUNCATE TABLE fee_config")
        connection.execute("""
            INSERT INTO fee_config (fee_payer, platform_fee_bps, min_fee_santim,
                                    max_fee_santim, vat_bps, effective_from)
            VALUES ('BUYER', 200, 500, 50000, 1500, now())
            """)


@pytest.fixture(autouse=True)
def clean_review_state(postgres_url: str) -> Iterator[None]:
    """Empty the review book around every test, so no test inherits a decision."""
    _reset_review_state(postgres_url)
    yield
    _reset_review_state(postgres_url)


def _token(client: TestClient, email: str, secret: bytes) -> str:
    response = client.post(
        "/v1/admin/login",
        json={
            "email": email,
            "password": STAFF_PASSWORD,
            "totp_code": staff_totp.current_code(secret, at=datetime.now(UTC)),
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["scope"] == "full", response.text
    return cast(str, response.json()["access_token"])


@pytest.fixture()
def maker_headers(client: TestClient, staff_secrets: dict[str, bytes]) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(client, MAKER_EMAIL, staff_secrets[MAKER_EMAIL])}"}


@pytest.fixture()
def checker_headers(client: TestClient, staff_secrets: dict[str, bytes]) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_token(client, CHECKER_EMAIL, staff_secrets[CHECKER_EMAIL])}"
    }


@pytest.fixture()
def support_headers(client: TestClient, staff_secrets: dict[str, bytes]) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_token(client, SUPPORT_EMAIL, staff_secrets[SUPPORT_EMAIL])}"
    }


def _change_body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "fee_payer": "BUYER",
        "platform_fee_bps": 250,
        "min_fee_santim": 500,
        "max_fee_santim": 60_000,
        "vat_bps": 1_500,
        "reason": "Finance approved the spring schedule.",
    }
    body.update(overrides)
    return body


def _submit(client: TestClient, headers: dict[str, str], **overrides: object) -> dict[str, object]:
    response = client.post(
        "/v1/admin/fees/changes", json=_change_body(**overrides), headers=headers
    )
    assert response.status_code == 201, response.text
    return cast(dict[str, object], response.json())


async def _token_async(http: httpx.AsyncClient, email: str, secret: bytes) -> str:
    response = await http.post(
        "/v1/admin/login",
        json={
            "email": email,
            "password": STAFF_PASSWORD,
            "totp_code": staff_totp.current_code(secret, at=datetime.now(UTC)),
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["scope"] == "full", response.text
    return cast(str, response.json()["access_token"])


async def _staff_actor(email: str) -> StaffUser:
    """Load one staff row, detached from the session that read it."""
    async with get_session_factory()() as session:
        actor = await StaffUserRepository(session).find_by_email(email)
    assert actor is not None, email
    return actor


async def _approve_after_barrier(
    barrier: asyncio.Barrier,
    change_id: uuid.UUID,
    actor: StaffUser,
) -> PendingChange:
    """Open a connection, wait for the other approver, then approve.

    Opening the session before the barrier means both transactions already have a connection
    when they are released; the barrier is what makes them contend for the proposal's row
    rather than take turns before either has started.
    """
    async with get_session_factory()() as session:
        await session.connection()
        await barrier.wait()
        service = MakerCheckerService(
            pending=PendingChangeRepository(session),
            fee_configs=FeeConfigRepository(session),
            audit=AuditLogRepository(session),
        )
        return await service.approve(change_id=change_id, actor=actor, ip="198.51.100.25")


# --------------------------------------------------------------------------- the tests


def test_submit_records_a_waiting_proposal_and_changes_no_money(
    client: TestClient,
    maker_headers: dict[str, str],
    sql: Callable[..., Rows],
) -> None:
    """The maker's POST creates a PENDING row and an audit row; the active policy is untouched."""
    change = _submit(client, maker_headers)

    assert change["status"] == "PENDING"
    assert change["kind"] == "FEE_CONFIG_CHANGE"
    payload = cast(dict[str, object], change["payload"])
    assert payload["platform_fee_bps"] == 250

    pending = sql("SELECT status, submitted_by FROM pending_changes")
    assert len(pending) == 1
    assert pending[0]["status"] == "PENDING"

    active = sql("SELECT platform_fee_bps, effective_to FROM fee_config WHERE effective_to IS NULL")
    assert len(active) == 1
    assert active[0] == {"platform_fee_bps": 200, "effective_to": None}

    audit = sql(
        "SELECT action, target_type, target_id, before_hash, after_hash"
        " FROM audit_log WHERE action = %s",
        (ACTION_SUBMITTED,),
    )
    assert len(audit) == 1
    assert audit[0]["target_type"] == "pending_change"
    assert str(audit[0]["target_id"]) == change["id"]
    assert audit[0]["before_hash"] is None
    assert audit[0]["after_hash"] is not None


def test_the_maker_cannot_approve_their_own_change(
    client: TestClient,
    maker_headers: dict[str, str],
    sql: Callable[..., Rows],
) -> None:
    """The same account that submitted the proposal is refused, with no state change."""
    change = _submit(client, maker_headers)

    refused = client.post(f"/v1/admin/fees/changes/{change['id']}/approve", headers=maker_headers)

    assert refused.status_code == 403
    assert refused.json()["error"]["details"]["reason"] == "self_review_forbidden"
    pending = sql("SELECT status, approved_by FROM pending_changes")
    assert pending == [{"status": "PENDING", "approved_by": None}]
    assert len(sql("SELECT id FROM fee_config WHERE effective_to IS NULL")) == 1
    assert len(sql("SELECT id FROM audit_log WHERE action = %s", (ACTION_APPROVED,))) == 0


def test_a_second_admin_approval_closes_the_old_policy_and_opens_the_new_one(
    client: TestClient,
    maker_headers: dict[str, str],
    checker_headers: dict[str, str],
    sql: Callable[..., Rows],
) -> None:
    """Approval is the transaction that applies the change, under a different account."""
    change = _submit(client, maker_headers)

    approved = client.post(
        f"/v1/admin/fees/changes/{change['id']}/approve", headers=checker_headers
    )

    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "APPROVED"

    active = sql(
        "SELECT fee_payer, platform_fee_bps, min_fee_santim, max_fee_santim, vat_bps"
        " FROM fee_config WHERE effective_to IS NULL"
    )
    assert active == [
        {
            "fee_payer": "BUYER",
            "platform_fee_bps": 250,
            "min_fee_santim": 500,
            "max_fee_santim": 60_000,
            "vat_bps": 1_500,
        }
    ]
    assert len(sql("SELECT id FROM fee_config WHERE effective_to IS NOT NULL")) == 1

    audit = sql(
        "SELECT before_hash, after_hash FROM audit_log WHERE action = %s",
        (ACTION_APPROVED,),
    )
    assert len(audit) == 1
    assert audit[0]["before_hash"] is not None
    assert audit[0]["after_hash"] is not None
    assert audit[0]["before_hash"] != audit[0]["after_hash"]


def test_the_editor_reports_the_new_policy_and_drops_the_applied_proposal(
    client: TestClient,
    maker_headers: dict[str, str],
    checker_headers: dict[str, str],
) -> None:
    """After approval the reader sees one current policy and no waiting proposal."""
    change = _submit(client, maker_headers)
    approved = client.post(
        f"/v1/admin/fees/changes/{change['id']}/approve", headers=checker_headers
    )
    assert approved.status_code == 200, approved.text

    read = client.get("/v1/admin/fees", headers=maker_headers)

    assert read.status_code == 200, read.text
    body = read.json()
    assert body["current"]["platform_fee_bps"] == 250
    assert body["current"]["effective_from"] is not None
    assert body["pending"] == []


def test_an_approved_change_prices_the_next_order(
    client: TestClient,
    maker_headers: dict[str, str],
    checker_headers: dict[str, str],
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
) -> None:
    """The editor is not a settings screen: an approval moves the next order's money."""
    change = _submit(
        client,
        maker_headers,
        platform_fee_bps=1_000,
        min_fee_santim=0,
        max_fee_santim=200_000,
        vat_bps=0,
    )
    approved = client.post(
        f"/v1/admin/fees/changes/{change['id']}/approve", headers=checker_headers
    )
    assert approved.status_code == 200, approved.text

    seller = signed_in()
    listing = client.post(
        "/v1/listings",
        headers=seller,
        json=listing_body(quantity_kg=1_000, min_order_kg=50, unit_price_santim=10_000),
    )
    assert listing.status_code == 201, listing.text
    buyer = signed_in("+251911000003")
    bought = client.post(
        f"/v1/listings/{listing.json()['id']}/buy-now",
        headers={**buyer, "Idempotency-Key": idempotency_key()},
        json=order_body(quantity_kg=100),
    )

    assert bought.status_code == 201, bought.text
    # 10% of 1,000,000 santim, with the approved change's zero VAT.
    assert bought.json()["platform_fee_santim"] == 100_000
    assert bought.json()["vat_santim"] == 0
    assert bought.json()["total_santim"] == 1_100_000


def test_rejection_needs_a_reason_and_leaves_the_policy_alone(
    client: TestClient,
    maker_headers: dict[str, str],
    checker_headers: dict[str, str],
    sql: Callable[..., Rows],
) -> None:
    """A checker may refuse, but the refusal is explained and changes no fee."""
    change = _submit(client, maker_headers)

    blank = client.post(
        f"/v1/admin/fees/changes/{change['id']}/reject",
        json={"reason": "   "},
        headers=checker_headers,
    )
    assert blank.status_code == 422

    rejected = client.post(
        f"/v1/admin/fees/changes/{change['id']}/reject",
        json={"reason": "Finance has not signed this version."},
        headers=checker_headers,
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == "REJECTED"
    assert rejected.json()["rejection_reason"] == "Finance has not signed this version."

    active = sql("SELECT platform_fee_bps FROM fee_config WHERE effective_to IS NULL")
    assert active == [{"platform_fee_bps": 200}]
    audit = sql(
        "SELECT before_hash, after_hash FROM audit_log WHERE action = %s",
        (ACTION_REJECTED,),
    )
    assert len(audit) == 1
    assert audit[0]["before_hash"] is not None
    assert audit[0]["after_hash"] is not None


def test_a_reviewed_proposal_cannot_be_reviewed_again(
    client: TestClient,
    maker_headers: dict[str, str],
    checker_headers: dict[str, str],
) -> None:
    """The second decision is a conflict, whichever direction it takes."""
    change = _submit(client, maker_headers)
    first = client.post(f"/v1/admin/fees/changes/{change['id']}/approve", headers=checker_headers)
    assert first.status_code == 200, first.text

    again = client.post(f"/v1/admin/fees/changes/{change['id']}/approve", headers=checker_headers)
    reject = client.post(
        f"/v1/admin/fees/changes/{change['id']}/reject",
        json={"reason": "Too late."},
        headers=checker_headers,
    )

    assert again.status_code == 409
    assert again.json()["error"]["details"]["reason"] == "already_reviewed"
    assert reject.status_code == 409


def test_an_unknown_proposal_is_not_found(
    client: TestClient,
    checker_headers: dict[str, str],
) -> None:
    """A random id is a 404, not a silent success or an internal error."""
    missing = uuid.uuid4()
    approve = client.post(f"/v1/admin/fees/changes/{missing}/approve", headers=checker_headers)
    reject = client.post(
        f"/v1/admin/fees/changes/{missing}/reject",
        json={"reason": "Nothing here."},
        headers=checker_headers,
    )

    assert approve.status_code == 404
    assert reject.status_code == 404


def test_every_fee_editor_route_is_admin_only(
    client: TestClient,
    maker_headers: dict[str, str],
    support_headers: dict[str, str],
) -> None:
    """SUPPORT may read the ledger, but not set the fee or sign for somebody else's proposal."""
    change = _submit(client, maker_headers)

    assert client.get("/v1/admin/fees", headers=support_headers).status_code == 403
    assert (
        client.post(
            "/v1/admin/fees/changes", json=_change_body(), headers=support_headers
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/v1/admin/fees/changes/{change['id']}/approve", headers=support_headers
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/v1/admin/fees/changes/{change['id']}/reject",
            json={"reason": "Not mine to sign."},
            headers=support_headers,
        ).status_code
        == 403
    )


def test_the_database_also_refuses_a_self_approval(
    sql: Callable[..., Rows],
    maker_headers: dict[str, str],
    client: TestClient,
) -> None:
    """A future applier that forgot the service check still cannot write a self-reviewed row."""
    change = _submit(client, maker_headers)

    with pytest.raises(psycopg.errors.CheckViolation):
        sql(
            "UPDATE pending_changes SET status = 'APPROVED', approved_by = submitted_by,"
            " approved_at = now() WHERE id = %s",
            (change["id"],),
        )


def test_the_audit_hashes_are_sha256_and_name_the_proposal(
    client: TestClient,
    maker_headers: dict[str, str],
    checker_headers: dict[str, str],
    sql: Callable[..., Rows],
) -> None:
    """The trail can be tied to the row and compared without either state being stored raw."""
    change = _submit(client, maker_headers)
    approved = client.post(
        f"/v1/admin/fees/changes/{change['id']}/approve", headers=checker_headers
    )
    assert approved.status_code == 200, approved.text

    rows = sql(
        "SELECT action, target_id, before_hash, after_hash FROM audit_log"
        " WHERE target_type = 'pending_change'"
    )
    by_action = {str(row["action"]): row for row in rows}

    assert set(by_action) == {ACTION_SUBMITTED, ACTION_APPROVED}
    assert all(str(row["target_id"]) == change["id"] for row in by_action.values())
    assert len(cast(str, by_action[ACTION_SUBMITTED]["after_hash"])) == 64
    assert by_action[ACTION_SUBMITTED]["before_hash"] is None
    assert len(cast(str, by_action[ACTION_APPROVED]["before_hash"])) == 64
    assert len(cast(str, by_action[ACTION_APPROVED]["after_hash"])) == 64


async def test_two_admins_racing_to_approve_apply_the_change_once(
    concurrent_client: httpx.AsyncClient,
    staff_secrets: dict[str, bytes],
    sql: Callable[..., Rows],
) -> None:
    """Two approvals released together contend on the row lock, so only one can apply.

    Without ``FOR UPDATE`` both readers would see ``PENDING`` and both would try to close the
    policy and open a new one. The barrier makes that a race rather than two calls in a row:
    both transactions already hold their connections before either is allowed to reach the
    proposal's row.
    """
    maker_token = await _token_async(concurrent_client, MAKER_EMAIL, staff_secrets[MAKER_EMAIL])
    submitted = await concurrent_client.post(
        "/v1/admin/fees/changes",
        json=_change_body(),
        headers={"Authorization": f"Bearer {maker_token}"},
    )
    assert submitted.status_code == 201, submitted.text
    submitted_body = submitted.json()
    change_id = uuid.UUID(submitted_body["id"])
    submitted_by = uuid.UUID(submitted_body["submitted_by"])

    approver_b = await _staff_actor(APPROVER_B_EMAIL)
    approver_c = await _staff_actor(APPROVER_C_EMAIL)
    assert approver_b.role is StaffRole.ADMIN
    assert approver_c.role is StaffRole.ADMIN
    assert approver_b.id != submitted_by
    assert approver_c.id != submitted_by

    barrier = asyncio.Barrier(2)
    results: list[PendingChange | BaseException] = list(
        await asyncio.gather(
            _approve_after_barrier(barrier, change_id, approver_b),
            _approve_after_barrier(barrier, change_id, approver_c),
            return_exceptions=True,
        )
    )
    winners = [result for result in results if isinstance(result, PendingChange)]
    losers = [result for result in results if isinstance(result, ApiError)]

    assert len(winners) == 1
    assert len(losers) == 1
    assert losers[0].code is ErrorCode.ILLEGAL_STATE_TRANSITION
    assert losers[0].details["reason"] == "already_reviewed"

    fee_rows = sql("SELECT platform_fee_bps, effective_to FROM fee_config")
    assert len(fee_rows) == 2
    assert [row["platform_fee_bps"] for row in fee_rows if row["effective_to"] is None] == [250]
    assert [row["platform_fee_bps"] for row in fee_rows if row["effective_to"] is not None] == [200]

    pending = sql("SELECT status, approved_by, rejected_by FROM pending_changes")
    assert len(pending) == 1
    assert pending[0]["status"] == "APPROVED"
    assert pending[0]["approved_by"] in {approver_b.id, approver_c.id}
    assert pending[0]["rejected_by"] is None

    audit = sql(
        "SELECT actor_id FROM audit_log WHERE action = %s",
        (ACTION_APPROVED,),
    )
    assert len(audit) == 1
    assert audit[0]["actor_id"] == pending[0]["approved_by"]
