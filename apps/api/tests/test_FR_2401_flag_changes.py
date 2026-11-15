"""FR-2401: changing a flag is a proposal, and a second admin is the only way to apply it.

The flag route is the fee route's queue with a different applier. This file drives the admin
routes end to end: a maker submits, the flag stays exactly where it was, and a different admin
either approves it into the table or rejects it without touching anything. A self-approval and
two approvers racing are the two ways the rule could be broken, so both are proven against the
database rather than only against the response.

The staff accounts are seeded once for the module because each seed is a subprocess. The review
book is emptied around every test, and the flags are put back to the migration's two disabled
rows, because these tests are about a change and must not inherit one.
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
import psycopg.rows
import pytest
from fastapi.testclient import TestClient

from apps.api.core import staff_totp
from apps.api.db.session import get_session_factory
from apps.api.models.pending_change import PendingChange
from apps.api.models.staff_user import StaffRole, StaffUser
from apps.api.repositories.audit_log_repository import AuditLogRepository
from apps.api.repositories.feature_flag_repository import FeatureFlagRepository
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

MAKER_EMAIL = "flag-maker-2401@gebeyalink.example"
CHECKER_EMAIL = "flag-checker-2401@gebeyalink.example"
APPROVER_B_EMAIL = "flag-approver-b-2401@gebeyalink.example"
APPROVER_C_EMAIL = "flag-approver-c-2401@gebeyalink.example"
SUPPORT_EMAIL = "flag-support-2401@gebeyalink.example"
STAFF_PASSWORD = "a-good-long-staff-password"

ACTION_SUBMITTED = "FLAG_CHANGE_SUBMITTED"
ACTION_APPROVED = "FLAG_CHANGE_APPROVED"
ACTION_REJECTED = "FLAG_CHANGE_REJECTED"

Rows = list[dict[str, object]]
Completed = subprocess.CompletedProcess[str]


def _sync_url(postgres_url: str) -> str:
    return postgres_url.replace("postgresql+psycopg://", "postgresql://")


@pytest.fixture()
def sql(postgres_url: str) -> Callable[[str, tuple[object, ...] | None], Rows]:
    """Run one statement straight against PostgreSQL and read any rows back."""
    sync_url = _sync_url(postgres_url)

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


def _reset_flags(sql: Callable[..., Rows]) -> None:
    """Put the two seeded flags back, disabled and unscoped, and remove invented ones."""
    sql("DELETE FROM feature_flags WHERE name NOT IN ('cod_enabled', 'escrow_enabled')")
    sql(
        "INSERT INTO feature_flags (name, enabled, description) VALUES"
        " ('cod_enabled', false, 'Whether checkout may create cash-on-delivery orders.'),"
        " ('escrow_enabled', false, 'Whether buyers may fund an order the platform holds"
        " until delivery.')"
        " ON CONFLICT (name) DO UPDATE SET enabled = false, percentage_rollout = NULL,"
        " region_targeting = NULL, updated_at = now()"
    )


def _reset_review_state(postgres_url: str) -> None:
    """Empty the review book and return the flags to their seeded state."""
    sync_url = _sync_url(postgres_url)
    with psycopg.connect(sync_url, autocommit=True) as connection:
        connection.execute("TRUNCATE TABLE audit_log")
        connection.execute("TRUNCATE TABLE pending_changes")
        connection.execute("TRUNCATE TABLE staff_sessions")
    with (
        psycopg.connect(sync_url, autocommit=True) as connection,
        connection.cursor(row_factory=psycopg.rows.dict_row) as cursor,
    ):
        cursor.execute(
            "DELETE FROM feature_flags WHERE name NOT IN" " ('cod_enabled', 'escrow_enabled')"
        )
        cursor.execute(
            "INSERT INTO feature_flags (name, enabled, description) VALUES"
            " ('cod_enabled', false, 'Whether checkout may create cash-on-delivery orders.'),"
            " ('escrow_enabled', false, 'Whether buyers may fund an order the platform holds"
            " until delivery.')"
            " ON CONFLICT (name) DO UPDATE SET enabled = false, percentage_rollout = NULL,"
            " region_targeting = NULL, updated_at = now()"
        )


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
    """The staff accounts these tests need, with known TOTP secrets."""
    sync_url = _sync_url(postgres_url)
    with psycopg.connect(sync_url, autocommit=True) as connection:
        connection.execute("TRUNCATE TABLE audit_log")
        connection.execute("TRUNCATE TABLE pending_changes")
        connection.execute("TRUNCATE TABLE staff_sessions")
        connection.execute("TRUNCATE TABLE staff_users CASCADE")

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


@pytest.fixture(autouse=True)
def clean_review_state(postgres_url: str) -> Iterator[None]:
    """Empty the review book around every test, so no test inherits a decision."""
    _reset_review_state(postgres_url)
    yield
    _reset_review_state(postgres_url)


@pytest.fixture()
def admin_client(client: TestClient, sql: Callable[..., Rows]) -> TestClient:
    """The application, with the flag rows put back after the client fixture opened escrow."""
    _reset_flags(sql)
    return client


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
def maker_headers(admin_client: TestClient, staff_secrets: dict[str, bytes]) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_token(admin_client, MAKER_EMAIL, staff_secrets[MAKER_EMAIL])}"
    }


@pytest.fixture()
def checker_headers(admin_client: TestClient, staff_secrets: dict[str, bytes]) -> dict[str, str]:
    return {
        "Authorization": (
            f"Bearer {_token(admin_client, CHECKER_EMAIL, staff_secrets[CHECKER_EMAIL])}"
        )
    }


@pytest.fixture()
def support_headers(admin_client: TestClient, staff_secrets: dict[str, bytes]) -> dict[str, str]:
    return {
        "Authorization": (
            f"Bearer {_token(admin_client, SUPPORT_EMAIL, staff_secrets[SUPPORT_EMAIL])}"
        )
    }


def _change_body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "name": "escrow_enabled",
        "enabled": True,
        "percentage_rollout": None,
        "region_targeting": None,
        "reason": "Treasury signed off the escrow pilot.",
    }
    body.update(overrides)
    return body


def _submit(client: TestClient, headers: dict[str, str], **overrides: object) -> dict[str, object]:
    response = client.post(
        "/v1/admin/flags/changes", json=_change_body(**overrides), headers=headers
    )
    assert response.status_code == 201, response.text
    return cast(dict[str, object], response.json())


def _flag_rows(sql: Callable[..., Rows]) -> dict[str, dict[str, object]]:
    return {
        cast(str, row["name"]): row
        for row in sql(
            "SELECT name, enabled, percentage_rollout, region_targeting FROM feature_flags"
        )
    }


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
    """Open a connection, wait for the other approver, then approve."""
    async with get_session_factory()() as session:
        await session.connection()
        await barrier.wait()
        service = MakerCheckerService(
            pending=PendingChangeRepository(session),
            fee_configs=FeeConfigRepository(session),
            feature_flags=FeatureFlagRepository(session),
            audit=AuditLogRepository(session),
        )
        return await service.approve(change_id=change_id, actor=actor, ip="198.51.100.25")


def test_submit_records_the_proposal_and_leaves_the_flag_alone(
    admin_client: TestClient,
    maker_headers: dict[str, str],
    sql: Callable[..., Rows],
) -> None:
    """The maker's POST is a PENDING row; the flag is still the migration's disabled row."""
    change = _submit(
        admin_client,
        maker_headers,
        percentage_rollout=25,
        region_targeting=["OR"],
    )

    assert change["kind"] == "FLAG_CHANGE"
    assert change["status"] == "PENDING"
    assert change["payload"] == {
        "name": "escrow_enabled",
        "enabled": True,
        "percentage_rollout": 25,
        "region_targeting": ["OR"],
    }
    assert _flag_rows(sql)["escrow_enabled"]["enabled"] is False

    audit = sql("SELECT action FROM audit_log WHERE target_type = 'pending_change'")
    assert [row["action"] for row in audit] == [ACTION_SUBMITTED]


def test_approval_applies_the_flag_and_the_editor_reads_it(
    admin_client: TestClient,
    maker_headers: dict[str, str],
    checker_headers: dict[str, str],
    sql: Callable[..., Rows],
) -> None:
    """A different admin's approval is the transaction that moves the row."""
    change = _submit(admin_client, maker_headers)

    approved = admin_client.post(
        f"/v1/admin/flags/changes/{change['id']}/approve", headers=checker_headers
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "APPROVED"

    flag = _flag_rows(sql)["escrow_enabled"]
    assert flag["enabled"] is True
    assert flag["percentage_rollout"] is None
    assert flag["region_targeting"] is None

    editor = admin_client.get("/v1/admin/flags", headers=checker_headers)
    assert editor.status_code == 200, editor.text
    body = editor.json()
    assert [row["name"] for row in body["flags"]] == ["cod_enabled", "escrow_enabled"]
    assert {row["name"]: row["enabled"] for row in body["flags"]} == {
        "cod_enabled": False,
        "escrow_enabled": True,
    }
    assert body["pending"] == []

    audit = sql("SELECT action FROM audit_log WHERE target_type = 'pending_change'")
    assert {row["action"] for row in audit} == {ACTION_SUBMITTED, ACTION_APPROVED}


def test_the_maker_cannot_approve_their_own_flag_change(
    admin_client: TestClient,
    maker_headers: dict[str, str],
    sql: Callable[..., Rows],
) -> None:
    """The proposer is refused before the applier, and the flag does not move."""
    change = _submit(admin_client, maker_headers)

    refused = admin_client.post(
        f"/v1/admin/flags/changes/{change['id']}/approve", headers=maker_headers
    )

    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "FORBIDDEN"
    assert _flag_rows(sql)["escrow_enabled"]["enabled"] is False
    pending = sql("SELECT status, approved_by FROM pending_changes")
    assert pending == [{"status": "PENDING", "approved_by": None}]


def test_rejection_closes_the_proposal_without_moving_the_flag(
    admin_client: TestClient,
    maker_headers: dict[str, str],
    checker_headers: dict[str, str],
    sql: Callable[..., Rows],
) -> None:
    """A no is a decision too, recorded with the reviewer's reason."""
    change = _submit(admin_client, maker_headers)

    rejected = admin_client.post(
        f"/v1/admin/flags/changes/{change['id']}/reject",
        json={"reason": "Hold the pilot until support is staffed."},
        headers=checker_headers,
    )

    assert rejected.status_code == 200, rejected.text
    body = rejected.json()
    assert body["status"] == "REJECTED"
    assert body["rejection_reason"] == "Hold the pilot until support is staffed."
    assert _flag_rows(sql)["escrow_enabled"]["enabled"] is False

    audit = sql("SELECT action FROM audit_log WHERE target_type = 'pending_change'")
    assert {row["action"] for row in audit} == {ACTION_SUBMITTED, ACTION_REJECTED}


def test_a_change_naming_a_flag_that_was_never_installed_is_not_found(
    admin_client: TestClient,
    maker_headers: dict[str, str],
) -> None:
    """The queue refuses to store a proposal whose applier would have no row to aim at."""
    response = admin_client.post(
        "/v1/admin/flags/changes",
        json=_change_body(name="mpesa_enabled"),
        headers=maker_headers,
    )

    assert response.status_code == 404, response.text
    error = response.json()["error"]
    assert error["code"] == "NOT_FOUND"
    assert error["details"] == {"resource": "feature_flag", "name": "mpesa_enabled"}


def test_a_percentage_outside_the_range_is_a_validation_error(
    admin_client: TestClient,
    maker_headers: dict[str, str],
) -> None:
    """A rollout is a percentage of users, not a number between zero and one hundred and one."""
    response = admin_client.post(
        "/v1/admin/flags/changes",
        json=_change_body(percentage_rollout=101),
        headers=maker_headers,
    )

    assert response.status_code == 422, response.text


def test_every_flag_editor_route_is_admin_only(
    admin_client: TestClient,
    maker_headers: dict[str, str],
    support_headers: dict[str, str],
) -> None:
    """SUPPORT may read the ledger, but a money gate is an ADMIN decision."""
    change = _submit(admin_client, maker_headers)

    assert admin_client.get("/v1/admin/flags", headers=support_headers).status_code == 403
    assert (
        admin_client.post(
            "/v1/admin/flags/changes", json=_change_body(), headers=support_headers
        ).status_code
        == 403
    )
    assert (
        admin_client.post(
            f"/v1/admin/flags/changes/{change['id']}/approve", headers=support_headers
        ).status_code
        == 403
    )
    assert (
        admin_client.post(
            f"/v1/admin/flags/changes/{change['id']}/reject",
            json={"reason": "Not mine to sign."},
            headers=support_headers,
        ).status_code
        == 403
    )


async def test_two_admins_racing_to_approve_apply_the_flag_once(
    concurrent_client: httpx.AsyncClient,
    staff_secrets: dict[str, bytes],
    sql: Callable[..., Rows],
) -> None:
    """Two approvals released together contend on the row lock, so only one applies."""
    _reset_flags(sql)
    maker_token = await _token_async(concurrent_client, MAKER_EMAIL, staff_secrets[MAKER_EMAIL])
    submitted = await concurrent_client.post(
        "/v1/admin/flags/changes",
        json=_change_body(),
        headers={"Authorization": f"Bearer {maker_token}"},
    )
    assert submitted.status_code == 201, submitted.text
    change_id = uuid.UUID(submitted.json()["id"])
    submitted_by = uuid.UUID(submitted.json()["submitted_by"])

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

    assert _flag_rows(sql)["escrow_enabled"]["enabled"] is True
    pending = sql("SELECT status, approved_by FROM pending_changes")
    assert len(pending) == 1
    assert pending[0]["status"] == "APPROVED"
    assert pending[0]["approved_by"] in {approver_b.id, approver_c.id}

    audit = sql("SELECT actor_id FROM audit_log WHERE action = %s", (ACTION_APPROVED,))
    assert len(audit) == 1
    assert audit[0]["actor_id"] == pending[0]["approved_by"]
