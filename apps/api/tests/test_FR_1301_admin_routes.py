"""FR-1301: the three pilot-critical staff routes.

The staff console needs to find a trader and change what the account may do, take a listing
off the board and put it back, and read the trail the other two write. This file proves those
three operations against PostgreSQL rather than against the response alone: the status a row
holds, the audit row a mutation leaves, and the state a refused transition did *not* change are
the facts the pilot actually depends on.

The audit log is append-only, so the fixtures truncate the staff tables between tests. The
last test then asks the database to rewrite a row this slice wrote, and expects the trigger to
refuse.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from urllib.parse import quote

import httpx
import psycopg
import psycopg.rows
import pytest
from fastapi.testclient import TestClient

from apps.api.core import staff_totp

pytestmark = [pytest.mark.integration]

REPO_ROOT = Path(__file__).resolve().parents[3]
GATEWAY_SECRET = "test-gateway-secret"
TERMS_VERSION = "2026-01-01"
TOTP_ENCRYPTION_KEY = "ZGVmZ2hpamtsbW5vcHFyc3R1dnd4eXphYmNkZWZnaGk="

ADMIN_EMAIL = "admin-routes@gebeyalink.example"
SUPPORT_EMAIL = "support-routes@gebeyalink.example"
MODERATOR_EMAIL = "moderator-routes@gebeyalink.example"
STAFF_PASSWORD = "a-good-long-staff-password"

STUDENT_PHONE = "+251911200001"
SECOND_PHONE = "+251911200002"
THIRD_PHONE = "+251911200003"
SELLER_PHONE = "+251911200010"

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
    """Empty the staff tables before each test.

    ``TRUNCATE`` is not caught by the row-level append-only trigger, which is why it is what a
    test may use and a runtime process may not.
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
    """Seed a staff account through the CLI, feeding the password on standard input."""
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
    """Sign in and change the password, returning a full-scoped token."""
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


@pytest.fixture()
def moderator_headers(cod_client: TestClient, run_cli: Callable[..., Completed]) -> dict[str, str]:
    return _staff_headers(cod_client, run_cli, email=MODERATOR_EMAIL, role="MODERATOR")


def _user_id(sql: Callable[..., Rows], phone_e164: str) -> str:
    rows = sql("SELECT id FROM users WHERE phone_e164 = %s", (phone_e164,))
    assert rows, phone_e164
    return str(rows[0]["id"])


def _admin_id(sql: Callable[..., Rows], email: str = ADMIN_EMAIL) -> str:
    rows = sql("SELECT id FROM staff_users WHERE email = %s", (email,))
    assert rows, email
    return str(rows[0]["id"])


def _audit_rows(sql: Callable[..., Rows], action: str) -> Rows:
    return sql(
        "SELECT actor_id, actor_role, action, target_type, target_id,"
        " before_hash, after_hash, ip FROM audit_log WHERE action = %s ORDER BY at",
        (action,),
    )


def _create_listing(
    client: TestClient,
    headers: dict[str, str],
    listing_body: Callable[..., dict[str, object]],
) -> str:
    response = client.post("/v1/listings", headers=headers, json=listing_body())
    assert response.status_code == 201, response.text
    return cast(str, response.json()["id"])


# ------------------------------------------------------------------- user search


def test_staff_can_search_users_by_phone_and_status_and_page(
    cod_client: TestClient,
    admin_headers: dict[str, str],
    cod_signed_in: Callable[..., dict[str, str]],
    sql: Callable[..., Rows],
) -> None:
    """The console's account list: all three filters and a keyset cursor that loses no row."""
    for phone in (STUDENT_PHONE, SECOND_PHONE, THIRD_PHONE):
        cod_signed_in(phone)
    third_id = _user_id(sql, THIRD_PHONE)
    frozen = cod_client.patch(
        f"/v1/admin/users/{third_id}",
        json={"status": "FROZEN", "reason": "support escalation"},
        headers=admin_headers,
    )
    assert frozen.status_code == 200, frozen.text

    all_users = cod_client.get("/v1/admin/users", headers=admin_headers)
    assert all_users.status_code == 200, all_users.text
    assert {row["phone_e164"] for row in all_users.json()["data"]} == {
        STUDENT_PHONE,
        SECOND_PHONE,
        THIRD_PHONE,
    }

    by_status = cod_client.get("/v1/admin/users?status=FROZEN", headers=admin_headers)
    assert by_status.status_code == 200, by_status.text
    assert [row["phone_e164"] for row in by_status.json()["data"]] == [THIRD_PHONE]

    by_prefix = cod_client.get("/v1/admin/users?phone_e164=%2B2519112000", headers=admin_headers)
    assert by_prefix.status_code == 200, by_prefix.text
    assert len(by_prefix.json()["data"]) == 3

    by_exact = cod_client.get(
        f"/v1/admin/users?phone_e164={STUDENT_PHONE.replace('+', '%2B')}",
        headers=admin_headers,
    )
    assert by_exact.status_code == 200, by_exact.text
    assert [row["phone_e164"] for row in by_exact.json()["data"]] == [STUDENT_PHONE]

    first_page = cod_client.get("/v1/admin/users?limit=2", headers=admin_headers)
    assert first_page.status_code == 200, first_page.text
    first_body = first_page.json()
    assert len(first_body["data"]) == 2
    assert first_body["has_more"] is True
    assert first_body["next_cursor"]

    second_page = cod_client.get(
        f"/v1/admin/users?limit=2&cursor={first_body['next_cursor']}",
        headers=admin_headers,
    )
    assert second_page.status_code == 200, second_page.text
    second_body = second_page.json()
    assert len(second_body["data"]) == 1
    assert second_body["has_more"] is False
    assert {row["id"] for row in first_body["data"]} & {
        row["id"] for row in second_body["data"]
    } == set()


# --------------------------------------------------------------- user mutations


def test_freezing_a_user_changes_the_row_and_writes_one_audit_row(
    cod_client: TestClient,
    admin_headers: dict[str, str],
    cod_signed_in: Callable[..., dict[str, str]],
    sql: Callable[..., Rows],
) -> None:
    """A ban is ``status=FROZEN`` with a reason, and the log names what changed."""
    cod_signed_in(STUDENT_PHONE)
    user_id = _user_id(sql, STUDENT_PHONE)

    response = cod_client.patch(
        f"/v1/admin/users/{user_id}",
        json={"status": "FROZEN", "reason": "repeated fraudulent listings"},
        headers=admin_headers,
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "FROZEN"
    assert sql("SELECT status FROM users WHERE id = %s", (user_id,))[0]["status"] == "FROZEN"

    audit = _audit_rows(sql, "USER_STATUS_CHANGED")
    assert len(audit) == 1
    assert str(audit[0]["actor_id"]) == _admin_id(sql)
    assert audit[0]["actor_role"] == "ADMIN"
    assert audit[0]["target_type"] == "user"
    assert str(audit[0]["target_id"]) == user_id
    assert audit[0]["before_hash"] is not None
    assert audit[0]["after_hash"] is not None
    assert audit[0]["before_hash"] != audit[0]["after_hash"]


def test_changing_a_primary_role_writes_one_audit_row(
    cod_client: TestClient,
    admin_headers: dict[str, str],
    cod_signed_in: Callable[..., dict[str, str]],
    sql: Callable[..., Rows],
) -> None:
    cod_signed_in(STUDENT_PHONE)
    user_id = _user_id(sql, STUDENT_PHONE)

    response = cod_client.patch(
        f"/v1/admin/users/{user_id}",
        json={"role": "BUYER"},
        headers=admin_headers,
    )

    assert response.status_code == 200, response.text
    assert response.json()["role"] == "BUYER"
    assert sql("SELECT role FROM users WHERE id = %s", (user_id,))[0]["role"] == "BUYER"
    audit = _audit_rows(sql, "USER_ROLE_CHANGED")
    assert len(audit) == 1
    assert str(audit[0]["target_id"]) == user_id


def test_additional_roles_are_added_and_removed_one_audit_row_each(
    cod_client: TestClient,
    admin_headers: dict[str, str],
    cod_signed_in: Callable[..., dict[str, str]],
    sql: Callable[..., Rows],
) -> None:
    """Additional roles are additions to the primary label, never a replacement for it."""
    cod_signed_in(STUDENT_PHONE)
    user_id = _user_id(sql, STUDENT_PHONE)

    added = cod_client.patch(
        f"/v1/admin/users/{user_id}",
        json={"additional_roles_add": ["AGENT", "COLLECTION_AGENT"]},
        headers=admin_headers,
    )
    assert added.status_code == 200, added.text
    assert set(added.json()["additional_roles"]) == {"AGENT", "COLLECTION_AGENT"}
    granted = sql(
        "SELECT role FROM user_roles WHERE user_id = %s AND revoked_at IS NULL", (user_id,)
    )
    assert {row["role"] for row in granted} == {"AGENT", "COLLECTION_AGENT"}
    assert len(_audit_rows(sql, "USER_ADDITIONAL_ROLE_ADDED")) == 2

    removed = cod_client.patch(
        f"/v1/admin/users/{user_id}",
        json={"additional_roles_remove": ["AGENT"]},
        headers=admin_headers,
    )
    assert removed.status_code == 200, removed.text
    assert removed.json()["additional_roles"] == ["COLLECTION_AGENT"]
    revoked = sql(
        "SELECT role, revoked_at FROM user_roles WHERE user_id = %s ORDER BY role", (user_id,)
    )
    by_role = {row["role"]: row["revoked_at"] for row in revoked}
    assert by_role["AGENT"] is not None
    assert by_role["COLLECTION_AGENT"] is None
    assert len(_audit_rows(sql, "USER_ADDITIONAL_ROLE_REMOVED")) == 1


def test_support_can_search_but_cannot_change_a_user(
    cod_client: TestClient,
    support_headers: dict[str, str],
    cod_signed_in: Callable[..., dict[str, str]],
    sql: Callable[..., Rows],
) -> None:
    """SUPPORT reads the account list; the account mutation is ADMIN-only."""
    cod_signed_in(STUDENT_PHONE)
    user_id = _user_id(sql, STUDENT_PHONE)

    assert cod_client.get("/v1/admin/users", headers=support_headers).status_code == 200
    refused = cod_client.patch(
        f"/v1/admin/users/{user_id}",
        json={"status": "FROZEN", "reason": "not allowed"},
        headers=support_headers,
    )

    assert refused.status_code == 403, refused.text
    assert sql("SELECT status FROM users WHERE id = %s", (user_id,))[0]["status"] == "ACTIVE"
    assert (
        sql("SELECT count(*) AS n FROM audit_log WHERE action = 'USER_STATUS_CHANGED'")[0]["n"] == 0
    )


# --------------------------------------------------------------- listing moderation


def test_suspending_an_active_listing_changes_status_and_audits(
    cod_client: TestClient,
    moderator_headers: dict[str, str],
    cod_signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    sql: Callable[..., Rows],
) -> None:
    """A moderator can take an ACTIVE listing off the board, and the reason is in the record."""
    seller = cod_signed_in(SELLER_PHONE)
    listing_id = _create_listing(cod_client, seller, listing_body)

    response = cod_client.post(
        f"/v1/admin/listings/{listing_id}/suspend",
        json={"reason": "misleading photographs"},
        headers=moderator_headers,
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "SUSPENDED"
    assert (
        sql("SELECT status FROM listings WHERE id = %s", (listing_id,))[0]["status"] == "SUSPENDED"
    )
    audit = _audit_rows(sql, "LISTING_SUSPENDED")
    assert len(audit) == 1
    assert audit[0]["actor_role"] == "MODERATOR"
    assert audit[0]["target_type"] == "listing"
    assert str(audit[0]["target_id"]) == listing_id
    assert audit[0]["before_hash"] is not None
    assert audit[0]["after_hash"] is not None


def test_suspending_a_paused_listing_is_an_illegal_transition(
    cod_client: TestClient,
    admin_headers: dict[str, str],
    cod_signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    sql: Callable[..., Rows],
) -> None:
    """The listing state table is the only authority; moderation does not invent an edge."""
    seller = cod_signed_in(SELLER_PHONE)
    listing_id = _create_listing(cod_client, seller, listing_body)
    paused = cod_client.post(f"/v1/listings/{listing_id}/pause", headers=seller)
    assert paused.status_code == 200, paused.text

    refused = cod_client.post(
        f"/v1/admin/listings/{listing_id}/suspend",
        json={"reason": "already paused"},
        headers=admin_headers,
    )

    assert refused.status_code == 409, refused.text
    error = refused.json()["error"]
    assert error["code"] == "ILLEGAL_STATE_TRANSITION"
    assert error["details"]["status"] == "PAUSED"
    assert error["details"]["requested"] == "SUSPENDED"
    assert sql("SELECT status FROM listings WHERE id = %s", (listing_id,))[0]["status"] == "PAUSED"
    assert _audit_rows(sql, "LISTING_SUSPENDED") == []


def test_restoring_a_suspended_listing_changes_status_and_audits(
    cod_client: TestClient,
    admin_headers: dict[str, str],
    cod_signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    sql: Callable[..., Rows],
) -> None:
    seller = cod_signed_in(SELLER_PHONE)
    listing_id = _create_listing(cod_client, seller, listing_body)
    suspended = cod_client.post(
        f"/v1/admin/listings/{listing_id}/suspend",
        json={"reason": "under review"},
        headers=admin_headers,
    )
    assert suspended.status_code == 200, suspended.text

    restored = cod_client.post(f"/v1/admin/listings/{listing_id}/restore", headers=admin_headers)

    assert restored.status_code == 200, restored.text
    assert restored.json()["status"] == "ACTIVE"
    assert sql("SELECT status FROM listings WHERE id = %s", (listing_id,))[0]["status"] == "ACTIVE"
    audit = _audit_rows(sql, "LISTING_RESTORED")
    assert len(audit) == 1
    assert str(audit[0]["target_id"]) == listing_id


# ------------------------------------------------------------------- audit search


def test_audit_search_filters_and_paginates(
    cod_client: TestClient,
    admin_headers: dict[str, str],
    cod_signed_in: Callable[..., dict[str, str]],
    sql: Callable[..., Rows],
) -> None:
    """The trail is searchable by actor, action, target and time, and pages without loss."""
    cod_signed_in(STUDENT_PHONE)
    user_id = _user_id(sql, STUDENT_PHONE)
    assert (
        cod_client.patch(
            f"/v1/admin/users/{user_id}",
            json={"status": "FROZEN", "reason": "review"},
            headers=admin_headers,
        ).status_code
        == 200
    )
    assert (
        cod_client.patch(
            f"/v1/admin/users/{user_id}",
            json={"role": "BUYER"},
            headers=admin_headers,
        ).status_code
        == 200
    )
    actor_id = _admin_id(sql)

    by_actor_and_action = cod_client.get(
        f"/v1/admin/audit-log?actor_id={actor_id}&action=USER_STATUS_CHANGED",
        headers=admin_headers,
    )
    assert by_actor_and_action.status_code == 200, by_actor_and_action.text
    assert len(by_actor_and_action.json()["data"]) == 1

    by_target = cod_client.get(
        f"/v1/admin/audit-log?target_type=user&target_id={user_id}",
        headers=admin_headers,
    )
    assert by_target.status_code == 200, by_target.text
    assert len(by_target.json()["data"]) == 2

    since = quote((datetime.now(UTC) - timedelta(minutes=1)).isoformat(), safe="")
    until = quote((datetime.now(UTC) + timedelta(minutes=1)).isoformat(), safe="")
    in_window = cod_client.get(
        f"/v1/admin/audit-log?actor_id={actor_id}&since={since}&until={until}",
        headers=admin_headers,
    )
    assert in_window.status_code == 200, in_window.text
    assert len(in_window.json()["data"]) == 2

    first_page = cod_client.get(
        f"/v1/admin/audit-log?actor_id={actor_id}&limit=1", headers=admin_headers
    )
    assert first_page.status_code == 200, first_page.text
    first_body = first_page.json()
    assert len(first_body["data"]) == 1
    assert first_body["has_more"] is True
    assert first_body["next_cursor"]

    second_page = cod_client.get(
        f"/v1/admin/audit-log?actor_id={actor_id}&limit=1&cursor={first_body['next_cursor']}",
        headers=admin_headers,
    )
    assert second_page.status_code == 200, second_page.text
    second_body = second_page.json()
    assert len(second_body["data"]) == 1
    assert second_body["has_more"] is False
    assert second_body["data"][0]["id"] != first_body["data"][0]["id"]


def test_a_trader_cannot_read_the_audit_log(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
) -> None:
    trader = cod_signed_in(STUDENT_PHONE)

    refused = cod_client.get("/v1/admin/audit-log", headers=trader)

    assert refused.status_code == 403, refused.text


# ------------------------------------------------------------------- append-only


def test_audit_rows_written_by_this_slice_are_append_only(
    cod_client: TestClient,
    admin_headers: dict[str, str],
    cod_signed_in: Callable[..., dict[str, str]],
    sql: Callable[..., Rows],
) -> None:
    """R5: the route's own audit row is still refused by the database's UPDATE trigger."""
    cod_signed_in(STUDENT_PHONE)
    user_id = _user_id(sql, STUDENT_PHONE)
    assert (
        cod_client.patch(
            f"/v1/admin/users/{user_id}",
            json={"status": "FROZEN", "reason": "append-only check"},
            headers=admin_headers,
        ).status_code
        == 200
    )

    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        sql("UPDATE audit_log SET action = 'rewritten' WHERE action = 'USER_STATUS_CHANGED'")
