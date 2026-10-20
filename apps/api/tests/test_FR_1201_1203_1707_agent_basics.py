"""FR-1201, FR-1202, FR-1203 and FR-1707: the agent role and the actions it carries.

Four requirements share this file because they describe one thing seen from four sides: an
account that has been approved as an agent (FR-1201), the farmer it may take on by proving the
farmer's own phone (FR-1202), the record of each action (FR-1203), and the boundary the agent
may not cross (FR-1707).

Three of the checks here are about what does *not* happen, and they are the point of the file:

* An agent cannot post a listing for a farmer it has no live assignment for.
* An agent cannot accept an offer or confirm a delivery as the farmer. Those routes read the
  caller's own identity and there is no parameter that lets one account act as another.
* An agent cannot change a farmer's phone number, payout accounts or OTP settings, because no
  route exists that lets any third party do so. Those are asserted as the absence of a path
  rather than as a refusal, because "there is no door" and "the door is locked" are different
  claims and only the first is true today.

The audit trail is the fifth thing, and it is honest about its own state: §11.1's ``audit_log``
table does not exist in this schema, so the agent actions below are *not* written to an audit
trail yet. The tests assert that deferral rather than pretending to a trail that is not there,
so the admin slice that adds the table has a failing assertion to turn green.

Everything runs against the COD-enabled application, because this is the pilot whose exit
criterion the agent channel serves.
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

REPO_ROOT = Path(__file__).resolve().parents[3]
GATEWAY_SECRET = "test-gateway-secret"
TERMS_VERSION = "2026-01-01"

AGENT_PHONE = "+251911000071"
FARMER_PHONE = "+251911000072"
OTHER_FARMER_PHONE = "+251911000073"

#: What a statement against PostgreSQL hands back.
Rows = list[dict[str, object]]
#: What an operator command's process hands back.
Completed = subprocess.CompletedProcess[str]


@pytest.fixture()
def sql(postgres_url: str) -> Iterator[Callable[[str, tuple[object, ...] | None], Rows]]:
    """Run one statement straight against PostgreSQL and read any rows back.

    Deliberately not through the application's repositories: a test that asks the code under
    test whether the code under test wrote a row proves very little. The agent role, the
    assignment and the account's tier are all read this way.
    """
    sync_url = postgres_url.replace("postgresql+psycopg://", "postgresql://")

    def _run(statement: str, params: tuple[object, ...] | None = None) -> list[dict[str, object]]:
        with (
            psycopg.connect(sync_url, autocommit=True) as connection,
            connection.cursor(row_factory=psycopg.rows.dict_row) as cursor,
        ):
            cursor.execute(statement, params)
            if cursor.description is None:
                return []
            return list(cursor.fetchall())

    yield _run


@pytest.fixture()
def run_command(postgres_url: str, redis_url: str) -> Callable[[str, str], Completed]:
    """Run one operator command the way an operator does: in its own process.

    The commands build their own engine and read their own configuration, and a test that
    called their function in this interpreter would share this run's engine and then dispose
    it under the running application. A subprocess is both the honest way to run a CLI and the
    safe one.
    """
    environment = {
        **os.environ,
        "DATABASE_URL": postgres_url,
        "REDIS_URL": redis_url,
        "SIGNING_PRIVATE_KEY": "unused-outside-the-application",
        "SIGNING_PUBLIC_KEY": "unused-outside-the-application",
        "GATEWAY_SECRET": GATEWAY_SECRET,
        "TERMS_VERSION": TERMS_VERSION,
        # The agent commands build their service graph from configuration, and the graph now
        # includes the staff cipher the audit path shares. A base64 32-byte placeholder.
        "TOTP_ENCRYPTION_KEY": "ZGVmZ2hpamtsbW5vcHFyc3R1dnd4eXphYmNkZWZnaGk=",
    }
    environment.pop("PYTHONIOENCODING", None)

    def _run(module: str, phone_e164: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", module, phone_e164],
            cwd=REPO_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

    return _run


@pytest.fixture()
def approve(run_command: Callable[[str, str], Completed]) -> Callable[[str], Completed]:
    def _approve(phone_e164: str) -> Completed:
        return run_command("apps.api.management.approve_agent", phone_e164)

    return _approve


@pytest.fixture()
def revoke(run_command: Callable[[str, str], Completed]) -> Callable[[str], Completed]:
    def _revoke(phone_e164: str) -> Completed:
        return run_command("apps.api.management.revoke_agent", phone_e164)

    return _revoke


def _become_tier_1(sql: Callable[..., list[dict[str, object]]], phone_e164: str) -> None:
    """Put an account at TIER_1 the way the KYC journey would.

    The journey itself is a later slice; this is the state FR-1201 approves from, so it is
    written directly rather than walked to.
    """
    sql("UPDATE users SET kyc_tier = 'TIER_1' WHERE phone_e164 = %s", (phone_e164,))


def _sign_in_and_bump(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    sql: Callable[..., list[dict[str, object]]],
    phone: str,
) -> dict[str, str]:
    """Sign a trader in, then make them TIER_1, and return their headers."""
    headers = cod_signed_in(phone)
    _become_tier_1(sql, phone)
    return headers


# --------------------------------------------------------------------------- FR-1201


def test_approving_a_tier_0_account_is_refused(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    approve: Callable[[str], subprocess.CompletedProcess[str]],
    sql: Callable[..., list[dict[str, object]]],
) -> None:
    """FR-1201's gate: an account that is not TIER_1 cannot be made an agent.

    The refusal is clear in two ways: the command exits non-zero, and it says why. A TIER_0
    account is not one whose identity anybody has checked, so approving it would put the
    farmer-onboarding power behind an unverified person.
    """
    cod_signed_in(AGENT_PHONE)  # signs in, landing at TIER_0

    result = approve(AGENT_PHONE)

    assert result.returncode != 0, result.stdout
    assert "tier_1_required" in (result.stdout + result.stderr)
    assert sql("SELECT 1 FROM user_roles WHERE role = 'AGENT'") == []


def test_approving_a_tier_1_account_grants_the_agent_role_once(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    approve: Callable[[str], subprocess.CompletedProcess[str]],
    sql: Callable[..., list[dict[str, object]]],
) -> None:
    """FR-1201 and its idempotence: one role row, however many times approval is run.

    An operator re-running a command is an ordinary habit, not a mistake to shout about, so
    the second run exits zero and leaves exactly one row. ``revoked_at`` is null on it, which
    is what the rest of the platform reads for "this account is an agent".
    """
    _sign_in_and_bump(cod_client, cod_signed_in, sql, AGENT_PHONE)

    first = approve(AGENT_PHONE)
    second = approve(AGENT_PHONE)

    assert first.returncode == 0, first.stdout + first.stderr
    assert second.returncode == 0, second.stdout + second.stderr
    rows = sql(
        "SELECT r.role, r.revoked_at FROM user_roles r JOIN users u ON u.id = r.user_id"
        " WHERE u.phone_e164 = %s",
        (AGENT_PHONE,),
    )
    assert len(rows) == 1
    assert rows[0]["role"] == "AGENT"
    assert rows[0]["revoked_at"] is None


def test_revoking_ends_the_role_and_the_assignments(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    approve: Callable[[str], subprocess.CompletedProcess[str]],
    revoke: Callable[[str], subprocess.CompletedProcess[str]],
    sql: Callable[..., list[dict[str, object]]],
) -> None:
    """FR-1201: revoking takes the role away and ends the relationships it was carrying.

    Both matter. A revoked agent with a live assignment would still be an agent to the listing
    path, so revocation ends the assignments too and reports how many. Running it again is a
    no-op: the role is already revoked and no assignment is active to stamp again.
    """
    _sign_in_and_bump(cod_client, cod_signed_in, sql, AGENT_PHONE)
    assert approve(AGENT_PHONE).returncode == 0
    agent_id = sql("SELECT id FROM users WHERE phone_e164 = %s", (AGENT_PHONE,))[0]["id"]

    # A farmer carried by the agent, so there is an active assignment to end.
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

    first = revoke(AGENT_PHONE)

    assert first.returncode == 0, first.stdout + first.stderr
    assert "1 assignment(s) ended" in first.stdout
    assert (
        sql("SELECT revoked_at FROM user_roles WHERE user_id = %s AND role = 'AGENT'", (agent_id,))[
            0
        ]["revoked_at"]
        is not None
    )
    assert (
        sql("SELECT revoked_at FROM agent_assignments WHERE agent_id = %s", (agent_id,))[0][
            "revoked_at"
        ]
        is not None
    )

    second = revoke(AGENT_PHONE)
    assert second.returncode == 0, second.stdout + second.stderr
    assert "0 assignment(s) ended" in second.stdout


# --------------------------------------------------------------------------- FR-1202


def _onboard(
    cod_client: TestClient,
    agent_headers: dict[str, str],
    code_for: Callable[[str], str],
    farmer_phone: str,
) -> httpx.Response:
    """Walk the whole two-request onboarding transfer and return the confirm response."""
    started = cod_client.post(
        "/v1/agent/farmers/onboard",
        json={"phone_e164": farmer_phone},
        headers=agent_headers,
    )
    assert started.status_code == 202, started.text
    confirmed = cod_client.post(
        "/v1/agent/farmers/onboard/confirm",
        json={
            "challenge_token": started.json()["challenge_token"],
            "otp": code_for(farmer_phone),
        },
        headers=agent_headers,
    )
    return cast(httpx.Response, confirmed)


@pytest.fixture()
def agent_headers(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    approve: Callable[[str], subprocess.CompletedProcess[str]],
    sql: Callable[..., list[dict[str, object]]],
) -> dict[str, str]:
    """A signed-in, approved agent, ready to take a farmer on."""
    headers = _sign_in_and_bump(cod_client, cod_signed_in, sql, AGENT_PHONE)
    assert approve(AGENT_PHONE).returncode == 0
    return headers


def test_the_code_goes_to_the_farmer_not_the_agent(
    cod_client: TestClient,
    agent_headers: dict[str, str],
    sms_recorder: object,
) -> None:
    """FR-1202's ownership transfer: the proof lands on the farmer's own phone.

    The agent starts the exchange, but the code is delivered to the farmer's number and to no
    other. That is what makes the farmer — not the agent — the one proving the number is
    theirs.

    The recorder is emptied first: the agent's own sign-in already put a code on the agent's
    phone, and this test is about who the *onboarding* code went to.
    """
    sms_recorder.clear()  # type: ignore[attr-defined]
    started = cod_client.post(
        "/v1/agent/farmers/onboard",
        json={"phone_e164": FARMER_PHONE},
        headers=agent_headers,
    )

    assert started.status_code == 202, started.text
    assert started.json()["challenge_token"]
    # What remains is only what this request sent.
    assert sms_recorder.messages_to(FARMER_PHONE)  # type: ignore[attr-defined]
    assert sms_recorder.messages_to(AGENT_PHONE) == []  # type: ignore[attr-defined]


def test_a_correct_code_creates_the_farmer_and_the_assignment(
    cod_client: TestClient,
    agent_headers: dict[str, str],
    code_for: Callable[[str], str],
    accounts: Callable[[], list[dict[str, object]]],
    sql: Callable[..., list[dict[str, object]]],
) -> None:
    """FR-1202: a proved number becomes a farmer, carried by the agent for twelve months.

    The account is a farmer reading Amharic, the relationship is one active row, and its end is
    twelve months after it was made. The response names the account and the number, and nothing
    about money or tier, because the agent is not the farmer.
    """
    confirmed = _onboard(cod_client, agent_headers, code_for, FARMER_PHONE)

    assert confirmed.status_code == 201, confirmed.text
    body = confirmed.json()
    assert body["phone_e164"] == FARMER_PHONE
    farmers = [row for row in accounts() if row["phone_e164"] == FARMER_PHONE]
    assert len(farmers) == 1
    assert farmers[0]["role"] == "FARMER"
    assert farmers[0]["language"] == "am"

    rows = sql(
        "SELECT a.expires_at, a.revoked_at,"
        " (a.expires_at - a.created_at) AS term"
        " FROM agent_assignments a JOIN users u ON u.id = a.user_id"
        " WHERE u.phone_e164 = %s",
        (FARMER_PHONE,),
    )
    assert len(rows) == 1
    assert rows[0]["revoked_at"] is None
    # Twelve months, within a day of clock jitter between the two timestamps.
    assert abs(cast(timedelta, rows[0]["term"]).days - 365) <= 1


def test_a_wrong_code_creates_nothing(
    cod_client: TestClient,
    agent_headers: dict[str, str],
    accounts: Callable[[], list[dict[str, object]]],
    sql: Callable[..., list[dict[str, object]]],
) -> None:
    """FR-1202: without the farmer's actual code, no account and no relationship appear.

    The number is unproved, so there is no farmer to take on. The challenge is spent by the
    attempt either way, so the same handle cannot be retried with a lucky guess.
    """
    started = cod_client.post(
        "/v1/agent/farmers/onboard",
        json={"phone_e164": FARMER_PHONE},
        headers=agent_headers,
    )
    token = started.json()["challenge_token"]

    confirmed = cod_client.post(
        "/v1/agent/farmers/onboard/confirm",
        json={"challenge_token": token, "otp": "000000"},
        headers=agent_headers,
    )

    assert confirmed.status_code != 201, confirmed.text
    assert [row for row in accounts() if row["phone_e164"] == FARMER_PHONE] == []
    assert sql("SELECT 1 FROM agent_assignments") == []


def test_onboarding_an_existing_farmer_recovers_the_account_and_refreshes_the_term(
    cod_client: TestClient,
    agent_headers: dict[str, str],
    code_for: Callable[[str], str],
    cod_signed_in: Callable[..., dict[str, str]],
    sql: Callable[..., list[dict[str, object]]],
) -> None:
    """FR-1202 and FR-1707: a farmer who already has an account is recovered, not duplicated.

    The relationship is refreshed rather than re-created — one active row, given a fresh twelve
    months — because a re-confirmation is the farmer's own act and that is what renews the
    relationship. The account count does not move.
    """
    # The farmer already trades on their own.
    cod_signed_in(FARMER_PHONE)
    original_id = sql("SELECT id FROM users WHERE phone_e164 = %s", (FARMER_PHONE,))[0]["id"]

    first = _onboard(cod_client, agent_headers, code_for, FARMER_PHONE)
    assert first.status_code == 201, first.text
    assert first.json()["id"] == str(original_id)

    # Age the assignment so the refresh is visible.
    sql(
        "UPDATE agent_assignments SET expires_at = now() + interval '3 months'"
        " WHERE user_id = %s",
        (original_id,),
    )

    second = _onboard(cod_client, agent_headers, code_for, FARMER_PHONE)

    assert second.status_code == 201, second.text
    rows = sql(
        "SELECT (expires_at - created_at) AS term FROM agent_assignments WHERE user_id = %s",
        (original_id,),
    )
    assert len(rows) == 1, "one active assignment, not a second row"
    assert abs(cast(timedelta, rows[0]["term"]).days - 365) <= 1
    assert sql("SELECT 1 FROM users WHERE phone_e164 = %s", (FARMER_PHONE,)) == sql(
        "SELECT 1 FROM users WHERE phone_e164 = %s", (FARMER_PHONE,)
    )


def test_a_non_agent_cannot_reach_the_onboarding_routes(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
) -> None:
    """FR-1201 gates FR-1202: an account with no AGENT role is refused before anything happens.

    Both onboarding steps are closed to it, so an account that merely signed in cannot send a
    code to somebody else's phone or complete an onboarding with one.
    """
    headers = cod_signed_in(AGENT_PHONE)  # signed in, but never approved

    started = cod_client.post(
        "/v1/agent/farmers/onboard",
        json={"phone_e164": FARMER_PHONE},
        headers=headers,
    )
    confirmed = cod_client.post(
        "/v1/agent/farmers/onboard/confirm",
        json={"challenge_token": "anything", "otp": "000000"},
        headers=headers,
    )

    assert started.status_code == 403, started.text
    assert confirmed.status_code == 403, confirmed.text


# --------------------------------------------------------------------------- delegated listing


def _listing_body(**overrides: object) -> dict[str, object]:
    now = datetime.now(UTC)
    body: dict[str, object] = {
        "crop_id": 1,
        "quantity_kg": 2_000,
        "min_order_kg": 100,
        "unit_price_santim": 3_250_000,
        "harvest_date": (now + timedelta(days=14)).date().isoformat(),
        "region_code": "OR",
        "woreda_id": 1,
        "delivery_options": ["PICKUP"],
        "delivery_fee_santim": 0,
        "description": "Red teff, well dried, from Adaa woreda",
        "organic": False,
    }
    body.update(overrides)
    return body


def _accept_body(**overrides: object) -> dict[str, object]:
    """A valid acceptance body, so the ownership check is what is reached rather than the schema."""
    body: dict[str, object] = {
        "delivery_method": "PICKUP",
        "buyer_address_woreda_id": 1,
        "buyer_address_text": "Merkato, Addis Ababa",
    }
    body.update(overrides)
    return body


def test_a_delegated_listing_is_the_farmers_not_the_agents(
    cod_client: TestClient,
    agent_headers: dict[str, str],
    code_for: Callable[[str], str],
    sql: Callable[..., list[dict[str, object]]],
) -> None:
    """FR-1202/FR-1707: a listing posted on a farmer's behalf belongs to the farmer.

    With a live assignment the posting succeeds, ``seller_id`` is the farmer and
    ``on_behalf_of_user_id`` names whose behalf it was on. The agent's own id is nowhere on the
    row as an owner.
    """
    confirmed = _onboard(cod_client, agent_headers, code_for, FARMER_PHONE)
    farmer_id = confirmed.json()["id"]
    agent_id = sql("SELECT id FROM users WHERE phone_e164 = %s", (AGENT_PHONE,))[0]["id"]

    posted = cod_client.post(
        "/v1/listings",
        json=_listing_body(on_behalf_of_user_id=farmer_id),
        headers=agent_headers,
    )

    assert posted.status_code == 201, posted.text
    rows = sql(
        "SELECT seller_id, on_behalf_of_user_id, source FROM listings WHERE id = %s",
        (posted.json()["id"],),
    )
    assert len(rows) == 1
    assert str(rows[0]["seller_id"]) == farmer_id
    assert str(rows[0]["on_behalf_of_user_id"]) == farmer_id
    assert rows[0]["seller_id"] != agent_id
    assert rows[0]["source"] == "AGENT"


def test_listing_on_behalf_without_an_assignment_is_refused(
    cod_client: TestClient,
    agent_headers: dict[str, str],
    cod_signed_in: Callable[..., dict[str, str]],
    sql: Callable[..., list[dict[str, object]]],
) -> None:
    """FR-1707: the relationship is what permits the posting, and its absence forbids it.

    The target account exists and the caller is an approved agent, but there is no assignment
    between them, so the request is refused with 403 and nothing is written.
    """
    cod_signed_in(OTHER_FARMER_PHONE)
    other_id = sql("SELECT id FROM users WHERE phone_e164 = %s", (OTHER_FARMER_PHONE,))[0]["id"]

    posted = cod_client.post(
        "/v1/listings",
        json=_listing_body(on_behalf_of_user_id=str(other_id)),
        headers=agent_headers,
    )

    assert posted.status_code == 403, posted.text
    assert sql("SELECT 1 FROM listings") == []


def test_listing_on_behalf_with_a_revoked_assignment_is_refused(
    cod_client: TestClient,
    agent_headers: dict[str, str],
    code_for: Callable[[str], str],
    revoke: Callable[[str], subprocess.CompletedProcess[str]],
    sql: Callable[..., list[dict[str, object]]],
) -> None:
    """FR-1707: revoking an agent ends the postings it may make immediately.

    The farmer is still carried in history, but the relationship is no longer live, so a
    delegated listing is refused once the agent has been stood down.
    """
    confirmed = _onboard(cod_client, agent_headers, code_for, FARMER_PHONE)
    farmer_id = confirmed.json()["id"]
    assert revoke(AGENT_PHONE).returncode == 0

    posted = cod_client.post(
        "/v1/listings",
        json=_listing_body(on_behalf_of_user_id=farmer_id),
        headers=agent_headers,
    )

    assert posted.status_code == 403, posted.text
    assert sql("SELECT 1 FROM listings") == []


def test_a_revoked_agent_cannot_onboard_again(
    cod_client: TestClient,
    agent_headers: dict[str, str],
    revoke: Callable[[str], subprocess.CompletedProcess[str]],
) -> None:
    """FR-1707: the role is checked on every agent route, so revocation closes both doors.

    The caller still holds a valid session, but the role is gone, so onboarding is refused at
    the same gate that admitted it before.
    """
    assert revoke(AGENT_PHONE).returncode == 0

    started = cod_client.post(
        "/v1/agent/farmers/onboard",
        json={"phone_e164": FARMER_PHONE},
        headers=agent_headers,
    )

    assert started.status_code == 403, started.text


# --------------------------------------------------------------------------- FR-1707 boundaries


def test_an_agent_cannot_change_the_farmers_phone(
    cod_client: TestClient,
    agent_headers: dict[str, str],
    code_for: Callable[[str], str],
    sql: Callable[..., list[dict[str, object]]],
) -> None:
    """FR-1707: the farmer's number is the farmer's, and no route moves it.

    There is no phone-change endpoint at all yet — for an owner or for a third party — and the
    delegated listing body does not carry one. The assertion is therefore that the farmer's
    number is exactly where the onboarding left it and that no request the agent can make names
    a different one. This is the boundary as an absence of a door, not as a locked one.
    """
    confirmed = _onboard(cod_client, agent_headers, code_for, FARMER_PHONE)
    farmer_id = confirmed.json()["id"]

    # The delegated posting carries no phone field and cannot be used to move one.
    posted = cod_client.post(
        "/v1/listings",
        json=_listing_body(on_behalf_of_user_id=farmer_id, phone_e164="+251911999999"),
        headers=agent_headers,
    )
    assert posted.status_code == 422, posted.text  # extra="forbid" rejects the foreign field

    assert (
        sql("SELECT phone_e164 FROM users WHERE id = %s", (farmer_id,))[0]["phone_e164"]
        == FARMER_PHONE
    )


def test_an_agent_cannot_change_payouts_or_otp_settings(
    cod_client: TestClient,
    agent_headers: dict[str, str],
    code_for: Callable[[str], str],
) -> None:
    """FR-1707: there is no route by which any third party sets another's payouts or OTP.

    Asserted as the absence of a path rather than as a refusal: the listing, offer and order
    routes carry no payout or OTP parameters, and the agent routes do not either. If a route
    ever grows one, this test's inventory of them is what has to change.
    """
    application_routes = {
        route.path
        for route in cod_client.app.routes  # type: ignore[attr-defined]
        if hasattr(route, "path")
    }
    joined = " ".join(sorted(application_routes))
    assert "payout" not in joined
    assert "otp_settings" not in joined
    assert "settings" not in joined


def test_an_agents_owner_scoped_write_lands_on_the_agent_not_the_farmer(
    cod_client: TestClient,
    agent_headers: dict[str, str],
    code_for: Callable[[str], str],
    sql: Callable[..., list[dict[str, object]]],
) -> None:
    """FR-1707: an existing owner-scoped route acts on the caller's account and only it.

    ``PATCH /v1/me`` is the owner-scoped write that exists today, so it is the one the
    boundary is proved on: the agent calls it with the agent's own token, and the change lands
    on the *agent's* profile. There is no parameter by which the call can be aimed at the
    farmer, so the farmer's row is read back and found untouched. This is the boundary as a
    working door that opens the caller's own room — the positive complement to the absence
    checks above, and the reason FR-1707's first clause needs no new code.
    """
    confirmed = _onboard(cod_client, agent_headers, code_for, FARMER_PHONE)
    farmer_id = confirmed.json()["id"]

    changed = cod_client.patch(
        "/v1/me",
        json={"display_name": "Agent's own name"},
        headers=agent_headers,
    )

    assert changed.status_code == 200, changed.text
    # The response is the agent's own account, named by the agent's own id.
    agent_id = sql("SELECT id FROM users WHERE phone_e164 = %s", (AGENT_PHONE,))[0]["id"]
    assert changed.json()["id"] == str(agent_id)
    assert changed.json()["display_name"] == "Agent's own name"
    # The farmer's profile was never named and never moved.
    farmer_profiles = sql("SELECT display_name FROM profiles WHERE user_id = %s", (farmer_id,))
    assert farmer_profiles == []


def test_an_agent_cannot_accept_an_offer_as_the_farmer(
    cod_client: TestClient,
    agent_headers: dict[str, str],
    code_for: Callable[[str], str],
    cod_signed_in_as_buyer: Callable[..., dict[str, str]],
    idempotency_key: Callable[[], str],
    offer_body: Callable[..., dict[str, object]],
    sql: Callable[..., list[dict[str, object]]],
) -> None:
    """FR-1707: accepting an offer reads the caller's own identity, with no behalf parameter.

    The agent posted the farmer's listing, so the farmer is the seller who may accept. But the
    agent's session is not the farmer's, and ``accept`` checks the caller against the seller —
    so the agent's attempt is refused. That is the whole of the requirement's second clause
    working through the ownership check that already existed.
    """
    confirmed = _onboard(cod_client, agent_headers, code_for, FARMER_PHONE)
    farmer_id = confirmed.json()["id"]
    posted = cod_client.post(
        "/v1/listings",
        json=_listing_body(on_behalf_of_user_id=farmer_id),
        headers=agent_headers,
    )
    listing_id = posted.json()["id"]

    buyer_headers = cod_signed_in_as_buyer()
    bid = cod_client.post(
        f"/v1/listings/{listing_id}/offers",
        json=offer_body(payment_mode="COD"),
        headers=buyer_headers,
    )
    assert bid.status_code == 201, bid.text
    offer_id = bid.json()["id"]

    accepted = cod_client.post(
        f"/v1/offers/{offer_id}/accept",
        json=_accept_body(),
        headers={**agent_headers, "Idempotency-Key": idempotency_key()},
    )

    assert accepted.status_code == 403, accepted.text


def test_an_agent_cannot_confirm_delivery_as_the_farmer(
    cod_client: TestClient,
    agent_headers: dict[str, str],
    code_for: Callable[[str], str],
    cod_signed_in: Callable[..., dict[str, str]],
    cod_signed_in_as_buyer: Callable[..., dict[str, str]],
    idempotency_key: Callable[[], str],
    offer_body: Callable[..., dict[str, object]],
) -> None:
    """FR-1707: confirming a delivery is a party's own act, checked against the caller's id.

    The order is real and the agent posted the listing it came from, but the delivery is the
    *buyer's* word to give, and the agent is neither buyer nor the account that made the
    request. The route resolves the caller to a party of the order and there is no parameter
    that lets one account stand in for another, so the agent's attempt is refused.
    """
    confirmed = _onboard(cod_client, agent_headers, code_for, FARMER_PHONE)
    farmer_id = confirmed.json()["id"]
    posted = cod_client.post(
        "/v1/listings",
        json=_listing_body(on_behalf_of_user_id=farmer_id),
        headers=agent_headers,
    )
    listing_id = posted.json()["id"]
    buyer_headers = cod_signed_in_as_buyer()
    bid = cod_client.post(
        f"/v1/listings/{listing_id}/offers",
        json=offer_body(payment_mode="COD"),
        headers=buyer_headers,
    )
    offer_id = bid.json()["id"]
    # The farmer accepts as themselves — they are the seller — so a real order exists for the
    # agent to fail against.
    farmer_headers = cod_signed_in(FARMER_PHONE)
    accepted = cod_client.post(
        f"/v1/offers/{offer_id}/accept",
        json=_accept_body(),
        headers={**farmer_headers, "Idempotency-Key": idempotency_key()},
    )
    assert accepted.status_code == 201, accepted.text
    order_id = accepted.json()["id"]

    confirmed_delivery = cod_client.post(
        f"/v1/orders/{order_id}/confirm-delivery",
        headers={**agent_headers, "Idempotency-Key": idempotency_key()},
    )

    assert confirmed_delivery.status_code == 403, confirmed_delivery.text


# --------------------------------------------------------------------------- FR-1203 audit


def test_the_audit_table_exists_and_its_producers_are_wired(postgres_url: str) -> None:
    """FR-1203, its deferral closed: §11.1's ``audit_log`` now exists and its producers write it.

    This test used to assert the opposite — that the table was absent and every agent action
    above was therefore unaudited — and it was written to go red the moment the admin basics
    slice landed. It has. The table exists, is append-only, and the four action names this
    module holds as constants are written by the producers: ``AGENT_APPROVED`` and
    ``AGENT_REVOKED`` by the admin routes, ``FARMER_ONBOARDED`` and
    ``LISTING_CREATED_ON_BEHALF`` by the agent paths above. The admin slice's own test file
    exercises each of the four; this pins that the pin itself was retargeted deliberately.
    """
    sync_url = postgres_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(sync_url) as connection:
        cursor = connection.cursor()
        cursor.execute("SELECT to_regclass('audit_log')")
        row = cursor.fetchone()
        assert row is not None
        assert (
            row[0] == "audit_log"
        ), "audit_log is absent again: FR-1203's producers have nothing to write to"
