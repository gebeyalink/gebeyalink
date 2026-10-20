"""FR-1301, FR-1302, SEC-12: staff sign-in, the audit log, and the queues that gate the pilot.

Three requirements share this file because they are one thing seen from three sides. FR-1301 is
the administrative surface — the KYC queue and the agent decisions. FR-1302 is the rule that
every administrative mutation is audited. SEC-12 is how a staff member proves themselves and
what a role change demands of them.

The file is deliberately heavy on "what was actually written": nearly every assertion reads
PostgreSQL directly rather than trusting a response, because the whole point of the audit log and
the append-only rule is that the *rows* are what is true, not what a handler returned.

Two facts shape the fixtures. First, the audit log is append-only — a test that wrote one row
cannot clean it up, so the tables are truncated between tests rather than the rows deleted.
Second, a staff session is ended by a row, not by a token, so several tests assert on
``staff_sessions.revoked_at`` rather than on a refused request alone.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import httpx
import psycopg
import psycopg.rows
import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from apps.api.core import staff_password, staff_totp
from apps.api.routers.admin import _ip

REPO_ROOT = Path(__file__).resolve().parents[3]
GATEWAY_SECRET = "test-gateway-secret"
TERMS_VERSION = "2026-01-01"
#: The Fernet key the whole test run shares. Same value the conftest hands the settings, so a
#: secret encrypted by one fixture is readable by the service another built.
TOTP_ENCRYPTION_KEY = "ZGVmZ2hpamtsbW5vcHFyc3R1dnd4eXphYmNkZWZnaGk="

ADMIN_EMAIL = "ada@gebeyalink.example"
SECOND_ADMIN_EMAIL = "grace@gebeyalink.example"
VERIFIER_EMAIL = "verifier@gebeyalink.example"
STAFF_PASSWORD = "a-good-long-staff-password"

AGENT_PHONE = "+251911000081"
FARMER_PHONE = "+251911000082"
OTHER_FARMER_PHONE = "+251911000083"

DOC_URI = "https://media.gebeyalink.example/kyc/doc.jpg"

Rows = list[dict[str, object]]
Completed = subprocess.CompletedProcess[str]


# --------------------------------------------------------------------------- fixtures


@pytest.fixture()
def sql(postgres_url: str) -> Iterator[Callable[[str, tuple[object, ...] | None], Rows]]:
    """Run one statement straight against PostgreSQL and read any rows back.

    Not through the application's repositories on purpose: a test that asks the code under test
    whether the code under test wrote a row proves very little. The audit rows, the staff rows,
    the sessions and the account's tier are all read this way.
    """
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

    The audit log cannot be cleaned the ordinary way: it is append-only, so a ``DELETE`` against
    it raises by design. ``TRUNCATE`` is a different statement and is not caught by a row-level
    ``BEFORE DELETE`` trigger, which is exactly why it is what a test may use and a runtime
    process may not. ``staff_sessions`` and ``staff_users`` are truncated with it, in the one
    order their foreign key requires.
    """
    sync_url = postgres_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(sync_url, autocommit=True) as connection:
        connection.execute("TRUNCATE TABLE audit_log")
        connection.execute("TRUNCATE TABLE staff_sessions")
        connection.execute("TRUNCATE TABLE staff_users CASCADE")
    yield


@pytest.fixture()
def run_cli(postgres_url: str, redis_url: str) -> Callable[..., Completed]:
    """Run an operator command the way an operator does: in its own process.

    A subprocess is both the honest way to run a CLI and the safe one: the commands build their
    own engine, and calling their function in this interpreter would share this run's engine and
    then dispose it under the running application.
    """
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


def _seed_admin(
    run_cli: Callable[..., Completed],
    *,
    email: str = ADMIN_EMAIL,
    role: str = "ADMIN",
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
    """The provisioning URI out of a seed command's output.

    ``next`` over the generator rather than a list slice: a seed that printed no URI should
    fail the test with a clear ``StopIteration`` rather than an ``IndexError`` on an empty list.
    """
    return next(line for line in stdout.splitlines() if line.startswith("otpauth://"))


def _secret_from_uri(uri: str) -> bytes:
    """The raw TOTP secret out of a provisioning URI, re-encoded from base32.

    The URI is the only place the secret leaves the server, so a test that has to produce valid
    codes reads exactly what an operator's authenticator would read.
    """
    import base64
    from urllib.parse import parse_qs, urlparse

    query = parse_qs(urlparse(uri).query)
    secret_b32 = query["secret"][0]
    padding = "=" * (-len(secret_b32) % 8)
    return base64.b32decode(secret_b32 + padding)


def _code(secret: bytes, *, at: datetime | None = None) -> str:
    """A valid code for a secret, for the test to present."""
    return staff_totp.current_code(secret, at=at)


@pytest.fixture()
def seeded_admin(run_cli: Callable[..., Completed]) -> bytes:
    """Seed an ADMIN and return its TOTP secret, read from the one-time provisioning URI."""
    result = _seed_admin(run_cli)
    assert result.returncode == 0, result.stdout + result.stderr
    return _secret_from_uri(_uri_from_stdout(result.stdout))


def _login(
    cod_client: TestClient,
    email: str,
    secret: bytes,
    *,
    password: str = STAFF_PASSWORD,
    code: str | None = None,
) -> httpx.Response:
    return cast(
        httpx.Response,
        cod_client.post(
            "/v1/admin/login",
            json={
                "email": email,
                "password": password,
                "totp_code": code if code is not None else _code(secret),
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


@pytest.fixture()
def admin_headers(cod_client: TestClient, seeded_admin: bytes) -> dict[str, str]:
    """Bearer headers for a signed-in, password-changed ADMIN.

    Depends on ``seeded_admin`` rather than seeding again: two fixtures that each seeded the
    same address would race the CLI's own "already exists" refusal.
    """
    token = _full_token(cod_client, ADMIN_EMAIL, seeded_admin, STAFF_PASSWORD)
    return {"Authorization": f"Bearer {token}"}


def _submit_kyc(client: TestClient, headers: dict[str, str], *, tier: str = "TIER_1") -> str:
    """Submit a KYC case as a trader and return its id."""
    response = client.post(
        "/v1/me/kyc",
        json={"tier_requested": tier, "doc_type": "FAYDA", "doc_images": [DOC_URI]},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return cast(str, response.json()["id"])


def _audit_rows(sql: Callable[..., Rows], action: str) -> Rows:
    return sql(
        "SELECT actor_id, actor_role, action, target_type, target_id,"
        " before_hash, after_hash, ip FROM audit_log WHERE action = %s ORDER BY at",
        (action,),
    )


# ------------------------------------------------------------ the scrypt and TOTP primitives


def test_password_round_trips_and_refuses_a_wrong_one() -> None:
    """The stored string is a scrypt string and nothing else reads it back."""
    stored = staff_password.hash_password("correct horse battery staple")
    assert stored.startswith("scrypt$")
    assert staff_password.verify_password("correct horse battery staple", stored)
    assert not staff_password.verify_password("correct horse battery stapl", stored)
    # A foreign or corrupt value is a failed verification, not an exception.
    assert not staff_password.verify_password("anything", "not-a-scrypt-string")
    assert not staff_password.verify_password("anything", "scrypt$1$2$3$@@$@@")


def test_the_stored_hash_carries_its_parameters() -> None:
    """A parameter bump is readable from the stored value, so a re-hash is decided, not guessed."""
    weak = staff_password.hash_password("a password", n=2**14, r=8, p=1)
    assert staff_password.needs_rehash(weak, n=2**17, r=8, p=1)
    strong = staff_password.hash_password("a password", n=2**17, r=8, p=1)
    assert not staff_password.needs_rehash(strong, n=2**17, r=8, p=1)
    # The weak hash still verifies — verification reads the parameters it was written with.
    assert staff_password.verify_password("a password", weak)


@pytest.mark.parametrize(
    ("timestamp", "expected"),
    [
        (59, "94287082"),
        (1111111109, "07081804"),
        (1111111111, "14050471"),
        (1234567890, "89005924"),
        (2000000000, "69279037"),
        (20000000000, "65353130"),
    ],
)
def test_totp_matches_the_rfc_6238_sha1_vectors(timestamp: int, expected: str) -> None:
    """RFC 6238 Appendix B, verbatim, for the SHA1 vectors.

    Those are the vectors that apply to this implementation: the RFC's SHA256 and SHA512
    vectors are outside scope because the implementation is SHA1-only, which is the algorithm a
    TOTP authenticator app uses.
    """
    secret = b"12345678901234567890"
    at = datetime.fromtimestamp(timestamp, UTC)
    assert staff_totp.verify(expected, secret, at=at, window=0, digits=8)
    assert staff_totp.current_code(secret, at=at, digits=8) == expected


def test_totp_window_accepts_one_step_either_side_and_no_more() -> None:
    """The ±1 window: a code is good for the previous, current and next step."""
    secret = staff_totp.generate_secret()
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    code = staff_totp.current_code(secret, at=now)
    assert staff_totp.verify(code, secret, at=now, window=1)
    assert staff_totp.verify(code, secret, at=now + timedelta(seconds=30), window=1)
    assert staff_totp.verify(code, secret, at=now - timedelta(seconds=30), window=1)
    # Two steps away is outside the window.
    assert not staff_totp.verify(code, secret, at=now + timedelta(seconds=60), window=1)
    assert not staff_totp.verify(code, secret, at=now - timedelta(seconds=60), window=1)


def test_fernet_round_trips_and_a_wrong_key_raises() -> None:
    """The TOTP secret is encrypted at rest, and a foreign key cannot read it."""
    from apps.api.core.staff_secrets import SecretUnreadableError, StaffSecretCipher

    cipher = StaffSecretCipher(TOTP_ENCRYPTION_KEY)
    secret = staff_totp.generate_secret()
    assert cipher.decrypt(cipher.encrypt(secret)) == secret

    other = StaffSecretCipher("MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=")
    with pytest.raises(SecretUnreadableError):
        other.decrypt(cipher.encrypt(secret))


# --------------------------------------------------------- the caller's address (the audit's ip)


def test_ip_takes_the_leftmost_forwarded_address() -> None:
    """The forwarding header wins, and the leftmost entry is the caller, not the proxies after."""
    request = Request(
        scope={
            "type": "http",
            "headers": [(b"x-forwarded-for", b"1.2.3.4, 5.6.7.8")],
            "client": ("10.0.0.1", 12345),
        }
    )
    assert _ip(request) == "1.2.3.4"


def test_ip_rejects_a_non_address() -> None:
    """A test client's host is a word, not an address; the ``inet`` column gets nothing."""
    request = Request(scope={"type": "http", "headers": [], "client": ("testclient", 12345)})
    assert _ip(request) is None


def test_ip_refuses_an_invalid_leftmost() -> None:
    """An invalid leftmost entry is not skipped for a later one: a guess is worse than nothing."""
    request = Request(
        scope={
            "type": "http",
            "headers": [(b"x-forwarded-for", b"not-an-ip, 1.2.3.4")],
            "client": ("10.0.0.1", 12345),
        }
    )
    assert _ip(request) is None


# ---------------------------------------------------------------------------- the seed command


def test_seed_creates_an_admin_and_writes_a_system_audit_row(
    run_cli: Callable[..., Completed], sql: Callable[..., Rows]
) -> None:
    """I.1: the first admin, its one-time URI, and a SYSTEM audit row."""
    result = _seed_admin(run_cli)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "otpauth://totp/" in result.stdout
    rows = sql("SELECT email, role, must_change_password FROM staff_users")
    assert len(rows) == 1
    assert rows[0]["email"] == ADMIN_EMAIL
    assert rows[0]["role"] == "ADMIN"
    assert rows[0]["must_change_password"] is True

    audit = _audit_rows(sql, "STAFF_CREATED")
    assert len(audit) == 1
    assert audit[0]["actor_id"] is None
    assert audit[0]["actor_role"] == "SYSTEM"


def test_seed_refuses_a_second_run_without_reset(
    run_cli: Callable[..., Completed], sql: Callable[..., Rows]
) -> None:
    """I: an operator's ordinary re-run must not silently overwrite a live credential."""
    assert _seed_admin(run_cli).returncode == 0

    result = _seed_admin(run_cli)

    assert result.returncode != 0
    assert "already exists" in (result.stdout + result.stderr)


# ------------------------------------------------------------------------------------ login


def test_login_requires_the_password_and_the_code(
    cod_client: TestClient, run_cli: Callable[..., Completed], sql: Callable[..., Rows]
) -> None:
    """SEC-12: both factors, and a disabled account is refused at the door."""
    result = _seed_admin(run_cli)
    uri = _uri_from_stdout(result.stdout)
    secret = _secret_from_uri(uri)

    ok = _login(cod_client, ADMIN_EMAIL, secret)
    assert ok.status_code == 200, ok.text
    assert ok.json()["scope"] == "change_password_only"

    # The session row exists and its expiry is the configured eight hours.
    sessions = sql("SELECT expires_at, created_at, revoked_at FROM staff_sessions")
    assert len(sessions) == 1
    assert sessions[0]["revoked_at"] is None
    span = cast(datetime, sessions[0]["expires_at"]) - cast(datetime, sessions[0]["created_at"])
    assert abs(span.total_seconds() - 8 * 60 * 60) < 5, span

    assert _login(cod_client, ADMIN_EMAIL, secret, password="the-wrong-password").status_code == 403
    assert _login(cod_client, ADMIN_EMAIL, secret, code="000000").status_code == 403
    assert _login(cod_client, "nobody@gebeyalink.example", secret).status_code == 403


def test_a_disabled_staff_member_cannot_log_in(
    cod_client: TestClient, run_cli: Callable[..., Completed], sql: Callable[..., Rows]
) -> None:
    """SEC-12/FR-1302: disabling is the off switch, and it closes the door."""
    result = _seed_admin(run_cli)
    uri = _uri_from_stdout(result.stdout)
    secret = _secret_from_uri(uri)
    sql("UPDATE staff_users SET disabled_at = now() WHERE email = %s", (ADMIN_EMAIL,))

    assert _login(cod_client, ADMIN_EMAIL, secret).status_code == 403


# ------------------------------------------------- change-password scope and session hygiene


def test_a_change_password_only_token_reaches_nothing_else(
    cod_client: TestClient, run_cli: Callable[..., Completed]
) -> None:
    """D: a must-change token is good for exactly one route."""
    result = _seed_admin(run_cli)
    uri = _uri_from_stdout(result.stdout)
    secret = _secret_from_uri(uri)
    token = _login(cod_client, ADMIN_EMAIL, secret).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    for method, path, body in (
        ("post", "/v1/admin/staff", {"email": "x@y.example", "role": "ADMIN"}),
        ("get", "/v1/admin/kyc-cases", None),
        ("post", "/v1/admin/agents/approve", {"user_id": str(uuid.uuid4())}),
    ):
        response = getattr(cod_client, method)(
            path, headers=headers, **({"json": body} if body else {})
        )
        assert response.status_code == 403, f"{path}: {response.text}"


def test_change_password_keeps_the_current_session_and_ends_the_others(
    cod_client: TestClient, run_cli: Callable[..., Completed], sql: Callable[..., Rows]
) -> None:
    """SEC-12: a password change closes every place the old password was signed in but this one."""
    result = _seed_admin(run_cli)
    uri = _uri_from_stdout(result.stdout)
    secret = _secret_from_uri(uri)
    first = _login(cod_client, ADMIN_EMAIL, secret)
    second = _login(cod_client, ADMIN_EMAIL, secret)
    assert first.status_code == 200 and second.status_code == 200

    changed = cod_client.post(
        "/v1/admin/change-password",
        json={
            "old_password": STAFF_PASSWORD,
            "new_password": "a-brand-new-long-password",
            "totp_code": _code(secret),
        },
        headers={"Authorization": f"Bearer {first.json()['access_token']}"},
    )
    assert changed.status_code == 204, changed.text

    rows = sql("SELECT id, revoked_at FROM staff_sessions ORDER BY created_at")
    assert rows[0]["revoked_at"] is None, "the changing session survives"
    assert rows[1]["revoked_at"] is not None, "the other session is revoked"
    assert sql("SELECT must_change_password FROM staff_users")[0]["must_change_password"] is False

    # The revoked token is refused — via the session row, not the JWT's own expiry.
    refused = cod_client.get(
        "/v1/admin/kyc-cases",
        headers={"Authorization": f"Bearer {second.json()['access_token']}"},
    )
    assert refused.status_code == 403, refused.text


# ----------------------------------------------------------------------------------- logout


def test_logout_revokes_the_session_row(
    cod_client: TestClient,
    admin_headers: dict[str, str],
    sql: Callable[..., Rows],
) -> None:
    """D: logout is a row that says the session is over, and the token stops working at once."""
    assert cod_client.post("/v1/admin/logout", headers=admin_headers).status_code == 204
    assert (
        sql("SELECT revoked_at FROM staff_sessions ORDER BY created_at DESC")[0]["revoked_at"]
        is not None
    )
    assert cod_client.get("/v1/admin/kyc-cases", headers=admin_headers).status_code == 403


# ------------------------------------------------------------------- staff administration


def test_creating_staff_returns_the_uri_once_and_audits(
    cod_client: TestClient, admin_headers: dict[str, str], sql: Callable[..., Rows]
) -> None:
    """D/E: the URI leaves the server once, and the creation is in the log."""
    response = cod_client.post(
        "/v1/admin/staff",
        json={"email": VERIFIER_EMAIL, "role": "VERIFIER"},
        headers=admin_headers,
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["provisioning_uri"].startswith("otpauth://totp/")
    assert body["generated_password"]
    assert body["must_change_password"] is True

    # No read route carries the URI again.
    read = cod_client.patch(
        f"/v1/admin/staff/{body['id']}", json={"disabled": False}, headers=admin_headers
    )
    assert "provisioning_uri" not in read.json()

    # The seed CLI also writes a STAFF_CREATED row — for the admin, as SYSTEM. The route's own
    # row is the one that names the member just created, and it is the only one for that target.
    audit = [
        row
        for row in _audit_rows(sql, "STAFF_CREATED")
        if row["target_id"] == uuid.UUID(body["id"])
    ]
    assert len(audit) == 1
    assert audit[0]["actor_role"] == "ADMIN"


def test_disabling_and_enabling_a_staff_member_is_audited(
    cod_client: TestClient, admin_headers: dict[str, str], sql: Callable[..., Rows]
) -> None:
    """FR-1302: the disable and the enable are each one row."""
    created = cod_client.post(
        "/v1/admin/staff", json={"email": VERIFIER_EMAIL, "role": "VERIFIER"}, headers=admin_headers
    ).json()

    disabled = cod_client.patch(
        f"/v1/admin/staff/{created['id']}", json={"disabled": True}, headers=admin_headers
    )
    enabled = cod_client.patch(
        f"/v1/admin/staff/{created['id']}", json={"disabled": False}, headers=admin_headers
    )

    assert disabled.status_code == 200 and enabled.status_code == 200
    assert len(_audit_rows(sql, "STAFF_DISABLED")) == 1
    assert len(_audit_rows(sql, "STAFF_ENABLED")) == 1


# ------------------------------------------------------------------------------ role changes


def test_an_admin_cannot_change_their_own_role(
    cod_client: TestClient, admin_headers: dict[str, str], sql: Callable[..., Rows]
) -> None:
    """E: an ADMIN promoting themselves is an escalation, and it is refused."""
    own_id = sql("SELECT id FROM staff_users WHERE email = %s", (ADMIN_EMAIL,))[0]["id"]

    response = cod_client.post(
        f"/v1/admin/staff/{own_id}/role", json={"role": "VERIFIER"}, headers=admin_headers
    )

    assert response.status_code == 403, response.text
    assert sql("SELECT role FROM staff_users WHERE id = %s", (own_id,))[0]["role"] == "ADMIN"


def test_a_role_change_revokes_the_target_sessions_and_audits_both_hashes(
    cod_client: TestClient, admin_headers: dict[str, str], sql: Callable[..., Rows]
) -> None:
    """SEC-12: the affected member must re-authenticate under the new role."""
    created = cod_client.post(
        "/v1/admin/staff",
        json={"email": VERIFIER_EMAIL, "role": "VERIFIER", "password": STAFF_PASSWORD},
        headers=admin_headers,
    ).json()
    uri = created["provisioning_uri"]
    secret = _secret_from_uri(uri)
    verifier_login = _login(cod_client, VERIFIER_EMAIL, secret, password=STAFF_PASSWORD)
    assert verifier_login.status_code == 200

    changed = cod_client.post(
        f"/v1/admin/staff/{created['id']}/role", json={"role": "SUPPORT"}, headers=admin_headers
    )

    assert changed.status_code == 200, changed.text
    role_now = sql("SELECT role FROM staff_users WHERE id = %s", (created["id"],))[0]["role"]
    assert role_now == "SUPPORT"
    # Every session the target held is revoked.
    assert (
        sql(
            "SELECT revoked_at FROM staff_sessions WHERE staff_user_id = %s",
            (created["id"],),
        )[
            0
        ]["revoked_at"]
        is not None
    )
    audit = _audit_rows(sql, "STAFF_ROLE_CHANGED")
    assert len(audit) == 1
    assert audit[0]["before_hash"] is not None and audit[0]["after_hash"] is not None
    # The old token no longer works.
    refused = cod_client.get(
        "/v1/admin/kyc-cases",
        headers={"Authorization": f"Bearer {verifier_login.json()['access_token']}"},
    )
    assert refused.status_code == 403, refused.text


# --------------------------------------------------------------------------------- the queue


def test_the_kyc_queue_lists_pending_cases_by_default(
    cod_client: TestClient,
    admin_headers: dict[str, str],
    cod_signed_in: Callable[..., dict[str, str]],
    sql: Callable[..., Rows],
) -> None:
    """F: the queue is the waiting cases, and it pages."""
    member = cod_signed_in(FARMER_PHONE)
    _submit_kyc(cod_client, member)

    listed = cod_client.get("/v1/admin/kyc-cases", headers=admin_headers)

    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert body["has_more"] is False
    assert len(body["data"]) == 1
    assert body["data"][0]["status"] == "PENDING"
    assert body["data"][0]["tier_requested"] == "TIER_1"


def test_the_queue_filters_by_tier(
    cod_client: TestClient,
    admin_headers: dict[str, str],
    cod_signed_in: Callable[..., dict[str, str]],
) -> None:
    """F: the tier filter is honoured."""
    first = cod_signed_in(FARMER_PHONE)
    _submit_kyc(cod_client, first, tier="TIER_1")

    filtered = cod_client.get(
        "/v1/admin/kyc-cases", params={"tier_requested": "TIER_2"}, headers=admin_headers
    )
    assert filtered.status_code == 200
    assert filtered.json()["data"] == []
    matching = cod_client.get(
        "/v1/admin/kyc-cases", params={"tier_requested": "TIER_1"}, headers=admin_headers
    )
    assert len(matching.json()["data"]) == 1


def test_approving_a_case_moves_the_tier_and_audits(
    cod_client: TestClient,
    admin_headers: dict[str, str],
    cod_signed_in: Callable[..., dict[str, str]],
    sql: Callable[..., Rows],
) -> None:
    """F: approval is the act that moves ``users.kyc_tier``, and it writes the before/after."""
    member = cod_signed_in(FARMER_PHONE)
    case_id = _submit_kyc(cod_client, member)

    approved = cod_client.post(f"/v1/admin/kyc-cases/{case_id}/approve", headers=admin_headers)

    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "APPROVED"
    assert approved.json()["reviewer_id"] is not None
    assert (
        sql("SELECT kyc_tier FROM users WHERE phone_e164 = %s", (FARMER_PHONE,))[0]["kyc_tier"]
        == "TIER_1"
    )
    audit = _audit_rows(sql, "KYC_APPROVED")
    assert len(audit) == 1
    assert audit[0]["before_hash"] is not None and audit[0]["after_hash"] is not None
    assert audit[0]["actor_role"] == "ADMIN"


def test_approving_a_decided_case_is_an_illegal_transition(
    cod_client: TestClient,
    admin_headers: dict[str, str],
    cod_signed_in: Callable[..., dict[str, str]],
) -> None:
    """F/R1: a non-PENDING case is 409 ILLEGAL_STATE_TRANSITION, not the submitter's KYC_PENDING."""
    member = cod_signed_in(FARMER_PHONE)
    case_id = _submit_kyc(cod_client, member)
    assert (
        cod_client.post(f"/v1/admin/kyc-cases/{case_id}/approve", headers=admin_headers).status_code
        == 200
    )

    again = cod_client.post(f"/v1/admin/kyc-cases/{case_id}/approve", headers=admin_headers)

    assert again.status_code == 409, again.text
    assert again.json()["error"]["code"] == "ILLEGAL_STATE_TRANSITION"


def test_rejecting_requires_a_reason_and_records_it(
    cod_client: TestClient,
    admin_headers: dict[str, str],
    cod_signed_in: Callable[..., dict[str, str]],
    sql: Callable[..., Rows],
) -> None:
    """F: the reason code is required, stored, and audited."""
    member = cod_signed_in(FARMER_PHONE)
    case_id = _submit_kyc(cod_client, member)

    missing = cod_client.post(
        f"/v1/admin/kyc-cases/{case_id}/reject", json={}, headers=admin_headers
    )
    assert missing.status_code == 422, missing.text

    rejected = cod_client.post(
        f"/v1/admin/kyc-cases/{case_id}/reject",
        json={"reason_code": "DOC_BLURRY"},
        headers=admin_headers,
    )

    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == "REJECTED"
    assert rejected.json()["rejection_reason"] == "DOC_BLURRY"
    # A non-fraud rejection leaves the tier and the status alone.
    assert (
        sql("SELECT kyc_tier, status FROM users WHERE phone_e164 = %s", (FARMER_PHONE,))[0][
            "kyc_tier"
        ]
        == "TIER_0"
    )
    audit = _audit_rows(sql, "KYC_REJECTED")
    assert len(audit) == 1


def test_a_fraud_suspicion_freezes_the_account(
    cod_client: TestClient,
    admin_headers: dict[str, str],
    cod_signed_in: Callable[..., dict[str, str]],
    sql: Callable[..., Rows],
) -> None:
    """F and §10.7: DOC_FRAUD_SUSPECTED is the one reason that acts on the person."""
    member = cod_signed_in(FARMER_PHONE)
    case_id = _submit_kyc(cod_client, member)

    rejected = cod_client.post(
        f"/v1/admin/kyc-cases/{case_id}/reject",
        json={"reason_code": "DOC_FRAUD_SUSPECTED"},
        headers=admin_headers,
    )

    assert rejected.status_code == 200, rejected.text
    assert (
        sql("SELECT status FROM users WHERE phone_e164 = %s", (FARMER_PHONE,))[0]["status"]
        == "FROZEN"
    )


# ------------------------------------------------------------------- agent administration


def test_agent_approve_requires_tier_1_and_is_idempotent(
    cod_client: TestClient,
    admin_headers: dict[str, str],
    cod_signed_in: Callable[..., dict[str, str]],
    sql: Callable[..., Rows],
) -> None:
    """G: TIER_0 refused with tier_1_required; TIER_1 granted once; both audited."""
    cod_signed_in(AGENT_PHONE)
    agent_id = sql("SELECT id FROM users WHERE phone_e164 = %s", (AGENT_PHONE,))[0]["id"]

    refused = cod_client.post(
        "/v1/admin/agents/approve", json={"user_id": str(agent_id)}, headers=admin_headers
    )
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["details"]["reason"] == "tier_1_required"

    sql("UPDATE users SET kyc_tier = 'TIER_1' WHERE id = %s", (agent_id,))
    first = cod_client.post(
        "/v1/admin/agents/approve", json={"user_id": str(agent_id)}, headers=admin_headers
    )
    second = cod_client.post(
        "/v1/admin/agents/approve", json={"user_id": str(agent_id)}, headers=admin_headers
    )

    assert first.status_code == 200 and second.status_code == 200
    roles = sql(
        "SELECT revoked_at FROM user_roles WHERE user_id = %s AND role = 'AGENT'", (agent_id,)
    )
    assert len(roles) == 1 and roles[0]["revoked_at"] is None
    assert len(_audit_rows(sql, "AGENT_APPROVED")) == 2


def test_agent_revoke_ends_the_role_and_the_assignments(
    cod_client: TestClient,
    admin_headers: dict[str, str],
    cod_signed_in: Callable[..., dict[str, str]],
    sql: Callable[..., Rows],
) -> None:
    """G: revocation takes the role away and ends the relationships it carried."""
    cod_signed_in(AGENT_PHONE)
    agent_id = sql("SELECT id FROM users WHERE phone_e164 = %s", (AGENT_PHONE,))[0]["id"]
    sql("UPDATE users SET kyc_tier = 'TIER_1' WHERE id = %s", (agent_id,))
    assert (
        cod_client.post(
            "/v1/admin/agents/approve", json={"user_id": str(agent_id)}, headers=admin_headers
        ).status_code
        == 200
    )

    farmer_id = uuid.uuid4()
    sql(
        "INSERT INTO users (id, phone_e164, role, language, kyc_tier, status)"
        " VALUES (%s, %s, 'FARMER', 'am', 'TIER_0', 'ACTIVE')",
        (farmer_id, FARMER_PHONE),
    )
    sql(
        "INSERT INTO agent_assignments (id, user_id, agent_id, expires_at)"
        " VALUES (%s, %s, %s, now() + interval '12 months')",
        (uuid.uuid4(), farmer_id, agent_id),
    )

    revoked = cod_client.post(f"/v1/admin/agents/{agent_id}/revoke", headers=admin_headers)

    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["assignments_ended"] == 1
    role_rows = sql(
        "SELECT revoked_at FROM user_roles WHERE user_id = %s AND role = 'AGENT'", (agent_id,)
    )
    assert role_rows[0]["revoked_at"] is not None
    assert (
        sql("SELECT revoked_at FROM agent_assignments WHERE agent_id = %s", (agent_id,))[0][
            "revoked_at"
        ]
        is not None
    )
    assert len(_audit_rows(sql, "AGENT_REVOKED")) == 1


def test_a_non_admin_cannot_reach_the_agent_routes(
    cod_client: TestClient,
    admin_headers: dict[str, str],
    sql: Callable[..., Rows],
) -> None:
    """G: the agent routes are ADMIN-only; a VERIFIER is refused."""
    created = cod_client.post(
        "/v1/admin/staff",
        json={"email": VERIFIER_EMAIL, "role": "VERIFIER", "password": STAFF_PASSWORD},
        headers=admin_headers,
    ).json()
    secret = _secret_from_uri(created["provisioning_uri"])
    verifier_token = _full_token(cod_client, VERIFIER_EMAIL, secret, STAFF_PASSWORD)
    headers = {"Authorization": f"Bearer {verifier_token}"}

    refused = cod_client.post(
        "/v1/admin/agents/approve", json={"user_id": str(uuid.uuid4())}, headers=headers
    )
    assert refused.status_code == 403, refused.text


# ------------------------------------------------------------------- append-only (R5)


def test_updating_the_audit_log_raises(sql: Callable[..., Rows]) -> None:
    """R5: the append-only trigger refuses an UPDATE, whatever the writer's role."""
    sql("INSERT INTO audit_log (actor_role, action, target_type) VALUES ('SYSTEM', 'X', 'y')")
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        sql("UPDATE audit_log SET action = 'rewritten'")


def test_deleting_from_the_audit_log_raises(sql: Callable[..., Rows]) -> None:
    """R5: and a DELETE."""
    sql("INSERT INTO audit_log (actor_role, action, target_type) VALUES ('SYSTEM', 'X', 'y')")
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        sql("DELETE FROM audit_log")


# ----------------------------------------------------- the pinning test, retargeted (R2/H)


def test_the_audit_table_exists_and_receives_the_agent_actions(postgres_url: str) -> None:
    """FR-1203, its deferral closed: ``audit_log`` exists and the producers are wired.

    The predecessor of this test asserted ``to_regclass('audit_log') IS NULL``; that is now
    false, which is exactly what the pin was for. The four action constants are exercised in the
    chain test at the bottom of this file; this asserts the table is present so the retarget is
    the deliberate act it was promised to be rather than a quietly deleted assertion.
    """
    sync_url = postgres_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(sync_url) as connection:
        cursor = connection.cursor()
        cursor.execute("SELECT to_regclass('audit_log')")
        row = cursor.fetchone()
        assert row is not None
        assert row[0] == "audit_log", "the pin is retargeted: the table now exists"


# ------------------------------------------------------------------- the whole chain


def test_the_whole_admin_chain_end_to_end(
    cod_client: TestClient,
    run_cli: Callable[..., Completed],
    cod_signed_in: Callable[..., dict[str, str]],
    code_for: Callable[[str], str],
    sql: Callable[..., Rows],
) -> None:
    """The pilot's chain, in one test: seed, sign in, administer, verify, approve, delegate.

    This is the assertion the pilot's hundred-listing target rests on: that a farmer can be
    onboarded by an agent who was approved through the audited route, after that farmer's tier
    was raised by a verifier through the audited queue, with every step written to the log.
    """
    # 1. Seed the first admin, sign in, and change the forced password.
    seeded = _seed_admin(run_cli)
    assert seeded.returncode == 0, seeded.stdout + seeded.stderr
    secret = _secret_from_uri(_uri_from_stdout(seeded.stdout))
    admin_token = _full_token(cod_client, ADMIN_EMAIL, secret, STAFF_PASSWORD)
    admin = {"Authorization": f"Bearer {admin_token}"}

    # 2. The admin creates a verifier through the audited route.
    verifier = cod_client.post(
        "/v1/admin/staff",
        json={"email": VERIFIER_EMAIL, "role": "VERIFIER", "password": STAFF_PASSWORD},
        headers=admin,
    )
    assert verifier.status_code == 201, verifier.text
    verifier_secret = _secret_from_uri(verifier.json()["provisioning_uri"])
    verifier_token = _full_token(cod_client, VERIFIER_EMAIL, verifier_secret, STAFF_PASSWORD)
    verifier_headers = {"Authorization": f"Bearer {verifier_token}"}

    # 3. A farmer submits KYC; the verifier approves it; the tier moves.
    farmer = cod_signed_in(FARMER_PHONE)
    case_id = _submit_kyc(cod_client, farmer)
    approved = cod_client.post(f"/v1/admin/kyc-cases/{case_id}/approve", headers=verifier_headers)
    assert approved.status_code == 200, approved.text
    assert (
        sql("SELECT kyc_tier FROM users WHERE phone_e164 = %s", (FARMER_PHONE,))[0]["kyc_tier"]
        == "TIER_1"
    )

    # 4. An agent (also TIER_1) is approved through the audited route, then onboards a farmer.
    cod_signed_in(AGENT_PHONE)
    agent_id = sql("SELECT id FROM users WHERE phone_e164 = %s", (AGENT_PHONE,))[0]["id"]
    sql("UPDATE users SET kyc_tier = 'TIER_1' WHERE id = %s", (agent_id,))
    assert (
        cod_client.post(
            "/v1/admin/agents/approve", json={"user_id": str(agent_id)}, headers=admin
        ).status_code
        == 200
    )

    agent_headers = cod_signed_in(AGENT_PHONE)
    started = cod_client.post(
        "/v1/agent/farmers/onboard",
        json={"phone_e164": OTHER_FARMER_PHONE},
        headers=agent_headers,
    )
    assert started.status_code == 202, started.text
    confirmed = cod_client.post(
        "/v1/agent/farmers/onboard/confirm",
        json={
            "challenge_token": started.json()["challenge_token"],
            "otp": code_for(OTHER_FARMER_PHONE),
        },
        headers=agent_headers,
    )
    assert confirmed.status_code == 201, confirmed.text

    # 5. The agent posts a listing on the farmer's behalf.
    listed = cod_client.post(
        "/v1/listings",
        json={
            "on_behalf_of_user_id": confirmed.json()["id"],
            "crop_id": 1,
            "quantity_kg": 2_000,
            "min_order_kg": 100,
            "unit_price_santim": 3_250_000,
            "harvest_date": (datetime.now(UTC) + timedelta(days=14)).date().isoformat(),
            "region_code": "OR",
            "woreda_id": 1,
            "delivery_options": ["PICKUP"],
            "delivery_fee_santim": 0,
            "description": "Red teff, well dried, from Adaa woreda",
            "organic": False,
        },
        headers=agent_headers,
    )
    assert listed.status_code == 201, listed.text

    # 6. Every action in the chain is in the log.
    for action in (
        "STAFF_CREATED",
        "KYC_APPROVED",
        "AGENT_APPROVED",
        "FARMER_ONBOARDED",
        "LISTING_CREATED_ON_BEHALF",
    ):
        assert _audit_rows(sql, action), action


def test_farmer_onboarded_and_delegated_listing_are_audited(
    cod_client: TestClient,
    admin_headers: dict[str, str],
    cod_signed_in: Callable[..., dict[str, str]],
    code_for: Callable[[str], str],
    sql: Callable[..., Rows],
) -> None:
    """H: the two agent actions that ship audited — onboarding and the delegated listing.

    Together with the AGENT_APPROVED assertion above, this is all four of the FR-1201-1203
    actions proving they write a row.
    """
    cod_signed_in(AGENT_PHONE)
    agent_id = sql("SELECT id FROM users WHERE phone_e164 = %s", (AGENT_PHONE,))[0]["id"]
    sql("UPDATE users SET kyc_tier = 'TIER_1' WHERE id = %s", (agent_id,))
    assert (
        cod_client.post(
            "/v1/admin/agents/approve", json={"user_id": str(agent_id)}, headers=admin_headers
        ).status_code
        == 200
    )
    agent_headers = cod_signed_in(AGENT_PHONE)

    # Onboard the farmer, whose code lands on the farmer's phone.
    started = cod_client.post(
        "/v1/agent/farmers/onboard",
        json={"phone_e164": FARMER_PHONE},
        headers=agent_headers,
    )
    assert started.status_code == 202, started.text
    confirmed = cod_client.post(
        "/v1/agent/farmers/onboard/confirm",
        json={"challenge_token": started.json()["challenge_token"], "otp": code_for(FARMER_PHONE)},
        headers=agent_headers,
    )
    assert confirmed.status_code == 201, confirmed.text

    onboarding = _audit_rows(sql, "FARMER_ONBOARDED")
    assert len(onboarding) == 1
    assert onboarding[0]["actor_role"] == "AGENT"
    assert onboarding[0]["target_id"] == uuid.UUID(confirmed.json()["id"])

    # A delegated listing, on the farmer's behalf.
    farmer_id = confirmed.json()["id"]
    from datetime import UTC as _UTC
    from datetime import datetime as _dt
    from datetime import timedelta as _td

    listed = cod_client.post(
        "/v1/listings",
        json={
            "on_behalf_of_user_id": farmer_id,
            "crop_id": 1,
            "quantity_kg": 2_000,
            "min_order_kg": 100,
            "unit_price_santim": 3_250_000,
            "harvest_date": (_dt.now(_UTC) + _td(days=14)).date().isoformat(),
            "region_code": "OR",
            "woreda_id": 1,
            "delivery_options": ["PICKUP"],
            "delivery_fee_santim": 0,
            "description": "Red teff, well dried, from Adaa woreda",
            "organic": False,
        },
        headers=agent_headers,
    )
    assert listed.status_code == 201, listed.text
    delegated = _audit_rows(sql, "LISTING_CREATED_ON_BEHALF")
    assert len(delegated) == 1
    assert delegated[0]["target_type"] == "listing"
    assert delegated[0]["target_id"] == uuid.UUID(listed.json()["id"])
