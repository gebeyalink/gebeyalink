"""FR-2512 and §10.7: a trader asking to be moved to a higher verification tier.

§6 governs how much a trader may trade, and the ceiling tracks ``users.kyc_tier``. This file is
about the *first* act of moving it: the account holder submits a document and names the tier
they are asking for, and a case is written waiting for a verifier. Nothing here moves a tier —
approval is the admin queue's, and that is a later slice — so almost every test below asserts a
``PENDING`` row beside an *unchanged* ``users.kyc_tier``, which is the shape the split between
submission and review is meant to produce.

The two refusals that carry the §6 ladder are stated as plainly as the acceptance:

* A tier that is not exactly the next rung is refused. Asking for the tier you already have is
  not a request to verify anything, and skipping a rung would ask a reviewer to check a
  document against a level the account never earned.
* A second case while one waits is refused. Two waiting cases for one account are two reviewers
  deciding the same question.

The tax number is the third thing: a business account's TIN travels *with* the case, is written
onto the profile in the same transaction, and is never seen apart from the case it arrived with.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

import psycopg
import psycopg.rows
import pytest
from fastapi.testclient import TestClient

Rows = list[dict[str, object]]

#: A document address, the shape a client's own photo storage hands back.
DOC_URI = "https://media.example.test/fayda-front.jpg"
SECOND_DOC_URI = "https://media.example.test/fayda-back.jpg"

MEMBER_PHONE = "+251911000081"
OTHER_MEMBER_PHONE = "+251911000082"


@pytest.fixture()
def sql(postgres_url: str) -> Iterator[Callable[[str, tuple[object, ...] | None], Rows]]:
    """Run one statement straight against PostgreSQL and read any rows back.

    Deliberately not through the application's repositories: a test that asks the code under
    test whether the code under test wrote a row proves very little. The case row, the account's
    tier and the profile's tax number are all read this way.
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


def _submission(**overrides: object) -> dict[str, object]:
    """A valid submission body, with any field replaced.

    TIER_1 by default, because that is the step every account takes first, and a FAYDA document
    because it is the ordinary path to it. A test about one rule should not have to restate the
    others.
    """
    body: dict[str, object] = {
        "tier_requested": "TIER_1",
        "doc_type": "FAYDA",
        "doc_images": [DOC_URI],
    }
    body.update(overrides)
    return body


def _tier_of(sql: Callable[..., Rows], phone_e164: str) -> object:
    return sql("SELECT kyc_tier FROM users WHERE phone_e164 = %s", (phone_e164,))[0]["kyc_tier"]


def _cases_for(sql: Callable[..., Rows], phone_e164: str) -> Rows:
    return sql(
        "SELECT c.status, c.tier_requested, c.doc_type, c.doc_images, c.rejection_reason"
        " FROM kyc_cases c JOIN users u ON u.id = c.user_id WHERE u.phone_e164 = %s"
        " ORDER BY c.created_at",
        (phone_e164,),
    )


# ------------------------------------------------------------------- the row that gets written


def test_a_submission_writes_a_pending_case_and_leaves_the_tier_alone(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    sql: Callable[..., Rows],
) -> None:
    """FR-2512: a submission opens a ``PENDING`` case, and the account is not moved by it.

    The two halves are the whole design of the split. The row exists — read back from
    PostgreSQL, not from the response — and the tier it asks for is *not* granted by the act of
    asking, because granting it here would make the review a formality.
    """
    headers = cod_signed_in(MEMBER_PHONE)

    response = cod_client.post("/v1/me/kyc", json=_submission(), headers=headers)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "PENDING"
    assert body["tier_requested"] == "TIER_1"
    assert body["doc_type"] == "FAYDA"
    assert "id" in body and "created_at" in body

    rows = _cases_for(sql, MEMBER_PHONE)
    assert len(rows) == 1
    assert rows[0]["status"] == "PENDING"
    assert rows[0]["tier_requested"] == "TIER_1"
    assert rows[0]["doc_images"] == [DOC_URI]
    # The account is exactly as the submission found it: still TIER_0 until a verifier decides.
    assert _tier_of(sql, MEMBER_PHONE) == "TIER_0"


def test_the_case_records_more_than_one_document(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    sql: Callable[..., Rows],
) -> None:
    """A case carries the whole set of addresses it was given, in order.

    A passport photographed open and a licence photographed front and back are the same shape:
    a short ordered list, and the order is what tells a reviewer which image is which.
    """
    headers = cod_signed_in(MEMBER_PHONE)

    response = cod_client.post(
        "/v1/me/kyc",
        json=_submission(doc_type="PASSPORT", doc_images=[DOC_URI, SECOND_DOC_URI]),
        headers=headers,
    )

    assert response.status_code == 201, response.text
    assert _cases_for(sql, MEMBER_PHONE)[0]["doc_images"] == [DOC_URI, SECOND_DOC_URI]


def test_a_case_may_carry_no_documents_yet(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    sql: Callable[..., Rows],
) -> None:
    """An empty list is a real state: a case opened before the photographs are attached.

    The column defaults to an empty list rather than to null, so "no images yet" is a value the
    schema holds rather than a missing one a reader has to interpret.
    """
    headers = cod_signed_in(MEMBER_PHONE)

    response = cod_client.post("/v1/me/kyc", json=_submission(doc_images=[]), headers=headers)

    assert response.status_code == 201, response.text
    assert _cases_for(sql, MEMBER_PHONE)[0]["doc_images"] == []


# --------------------------------------------------------------------- the §6 ladder is ordered


def test_asking_for_the_tier_already_held_is_refused(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    sql: Callable[..., Rows],
) -> None:
    """FR-2512: a request for the tier an account already has is not a request to verify it.

    Refused as a validation error, and nothing is written: a case that asks for the tier already
    held would be a reviewer reading a question nobody asked.
    """
    headers = cod_signed_in(MEMBER_PHONE)
    sql("UPDATE users SET kyc_tier = 'TIER_1' WHERE phone_e164 = %s", (MEMBER_PHONE,))

    response = cod_client.post(
        "/v1/me/kyc", json=_submission(tier_requested="TIER_1"), headers=headers
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["details"]["fields"]["tier_requested"] == (
        "must_be_the_next_tier_up"
    )
    assert _cases_for(sql, MEMBER_PHONE) == []


def test_a_tier_0_account_may_not_skip_straight_to_tier_2(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    sql: Callable[..., Rows],
) -> None:
    """§6's ladder is climbed a rung at a time: TIER_2 is not reachable from TIER_0.

    The rule is "strictly higher", so a single-step jump reads as valid against a bare
    comparison. It is refused because §6 verifies the *step*, and a submission that skipped a
    rung would ask a reviewer to check a document against a level the account never earned.
    """
    headers = cod_signed_in(MEMBER_PHONE)

    response = cod_client.post(
        "/v1/me/kyc", json=_submission(tier_requested="TIER_2"), headers=headers
    )

    assert response.status_code == 422, response.text
    assert _cases_for(sql, MEMBER_PHONE) == []


def test_a_tier_2_request_from_a_tier_1_account_is_accepted(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    sql: Callable[..., Rows],
) -> None:
    """The next rung up is accepted: TIER_2 from a TIER_1 account is a real step.

    This is the other side of the refusal above — the rule is about *position*, not about which
    tier is named — so the same request that a TIER_0 account is refused is taken from an
    account that has already earned TIER_1.
    """
    headers = cod_signed_in(MEMBER_PHONE)
    sql("UPDATE users SET kyc_tier = 'TIER_1' WHERE phone_e164 = %s", (MEMBER_PHONE,))

    response = cod_client.post(
        "/v1/me/kyc", json=_submission(tier_requested="TIER_2"), headers=headers
    )

    assert response.status_code == 201, response.text
    assert _cases_for(sql, MEMBER_PHONE)[0]["tier_requested"] == "TIER_2"


# --------------------------------------------------------------------------- the shape's own rules


def test_an_unknown_document_kind_is_refused(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
) -> None:
    """A document kind the platform does not accept is refused before it reaches the service."""
    headers = cod_signed_in(MEMBER_PHONE)

    response = cod_client.post(
        "/v1/me/kyc", json=_submission(doc_type="AFFIDAVIT"), headers=headers
    )

    assert response.status_code == 422, response.text


def test_more_than_three_documents_is_refused(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
) -> None:
    """§11.1: at most three photographs per case, enforced by the shape."""
    headers = cod_signed_in(MEMBER_PHONE)

    response = cod_client.post(
        "/v1/me/kyc",
        json=_submission(doc_images=[DOC_URI, SECOND_DOC_URI, DOC_URI, SECOND_DOC_URI]),
        headers=headers,
    )

    assert response.status_code == 422, response.text


def test_a_document_address_that_is_not_a_uri_is_refused(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
) -> None:
    """A document is fetched from a web address; anything else is refused at the shape."""
    headers = cod_signed_in(MEMBER_PHONE)

    response = cod_client.post(
        "/v1/me/kyc", json=_submission(doc_images=["not a url"]), headers=headers
    )

    assert response.status_code == 422, response.text


def test_a_tax_number_longer_than_the_ceiling_is_refused(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    sql: Callable[..., Rows],
) -> None:
    """§11.1 bounds the TIN, and a value past the ceiling is refused with nothing written."""
    headers = cod_signed_in(MEMBER_PHONE)

    response = cod_client.post(
        "/v1/me/kyc", json=_submission(tax_identification_number="9" * 31), headers=headers
    )

    assert response.status_code == 422, response.text
    assert _cases_for(sql, MEMBER_PHONE) == []


# --------------------------------------------------------------------------- one case at a time


def test_a_second_case_while_one_waits_is_refused(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    sql: Callable[..., Rows],
) -> None:
    """FR-2512: an account with a waiting case may not open a second.

    The refusal is a 409 rather than a 422, because nothing about the request is malformed: it
    is the account's *state* that forbids it, and a client needs to be able to tell the two
    apart. Exactly one row remains.
    """
    headers = cod_signed_in(MEMBER_PHONE)
    assert cod_client.post("/v1/me/kyc", json=_submission(), headers=headers).status_code == 201

    response = cod_client.post("/v1/me/kyc", json=_submission(), headers=headers)

    assert response.status_code == 409, response.text
    assert len(_cases_for(sql, MEMBER_PHONE)) == 1


def test_a_rejected_case_does_not_block_a_fresh_submission(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    sql: Callable[..., Rows],
) -> None:
    """A rejection is history, not a bar: the trader may correct their document and ask again.

    A decided case is what a reviewer has already read, so the rule that limits an account to
    one *waiting* case has nothing to say about it.
    """
    headers = cod_signed_in(MEMBER_PHONE)
    assert cod_client.post("/v1/me/kyc", json=_submission(), headers=headers).status_code == 201
    sql(
        "UPDATE kyc_cases SET status = 'REJECTED', rejection_reason = 'DOC_BLURRY'"
        " WHERE user_id = (SELECT id FROM users WHERE phone_e164 = %s)",
        (MEMBER_PHONE,),
    )

    response = cod_client.post("/v1/me/kyc", json=_submission(), headers=headers)

    assert response.status_code == 201, response.text
    assert len(_cases_for(sql, MEMBER_PHONE)) == 2


def test_an_approved_case_only_unblocks_a_higher_tier(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    sql: Callable[..., Rows],
) -> None:
    """An approval lets the account ask for the *next* tier, not the one it already earned.

    The two refusals stand together: once TIER_1 is approved the account may ask for TIER_2,
    but asking for TIER_1 again is refused because the account already holds it.
    """
    headers = cod_signed_in(MEMBER_PHONE)
    assert cod_client.post("/v1/me/kyc", json=_submission(), headers=headers).status_code == 201
    sql(
        "UPDATE kyc_cases SET status = 'APPROVED'"
        " WHERE user_id = (SELECT id FROM users WHERE phone_e164 = %s)",
        (MEMBER_PHONE,),
    )
    sql("UPDATE users SET kyc_tier = 'TIER_1' WHERE phone_e164 = %s", (MEMBER_PHONE,))

    same_tier = cod_client.post(
        "/v1/me/kyc", json=_submission(tier_requested="TIER_1"), headers=headers
    )
    higher_tier = cod_client.post(
        "/v1/me/kyc", json=_submission(tier_requested="TIER_2"), headers=headers
    )

    assert same_tier.status_code == 422, same_tier.text
    assert higher_tier.status_code == 201, higher_tier.text


# ------------------------------------------------------------- the tax number rides with the case


def test_a_tax_number_is_written_to_the_profile_with_the_case(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    sql: Callable[..., Rows],
) -> None:
    """FR-2512: a business account's TIN lands on its profile, in the same transaction.

    A profile may not exist yet for an account that has only ever signed in, so the row is
    created, and the case and the profile are both present afterwards — they travelled together
    or not at all.
    """
    headers = cod_signed_in(MEMBER_PHONE)

    response = cod_client.post(
        "/v1/me/kyc",
        json=_submission(doc_type="BUSINESS_LICENSE", tax_identification_number="1234567890"),
        headers=headers,
    )

    assert response.status_code == 201, response.text
    assert len(_cases_for(sql, MEMBER_PHONE)) == 1
    stored = sql(
        "SELECT p.tax_identification_number FROM profiles p"
        " JOIN users u ON u.id = p.user_id WHERE u.phone_e164 = %s",
        (MEMBER_PHONE,),
    )
    assert stored[0]["tax_identification_number"] == "1234567890"


def test_a_tax_number_does_not_disturb_the_rest_of_the_profile(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    sql: Callable[..., Rows],
) -> None:
    """The TIN write is a merge, not a replacement: what was already there stays.

    The submission writes one field, and an account that has filled in a display name and a
    region must not find them cleared by a submission that never mentioned them.
    """
    headers = cod_signed_in(MEMBER_PHONE)
    sql(
        "INSERT INTO profiles (id, user_id, display_name, region_code)"
        " VALUES (gen_random_uuid(), (SELECT id FROM users WHERE phone_e164 = %s), %s, 'OR')",
        (MEMBER_PHONE, "Lik Tessema"),
    )

    response = cod_client.post(
        "/v1/me/kyc",
        json=_submission(doc_type="BUSINESS_LICENSE", tax_identification_number="9988776655"),
        headers=headers,
    )

    assert response.status_code == 201, response.text
    stored = sql(
        "SELECT p.display_name, p.region_code, p.tax_identification_number FROM profiles p"
        " JOIN users u ON u.id = p.user_id WHERE u.phone_e164 = %s",
        (MEMBER_PHONE,),
    )
    assert stored[0]["display_name"] == "Lik Tessema"
    assert stored[0]["region_code"] == "OR"
    assert stored[0]["tax_identification_number"] == "9988776655"


def test_a_submission_without_a_tax_number_writes_no_profile(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    sql: Callable[..., Rows],
) -> None:
    """No TIN, no profile row: a submission is not a reason to create a profile on its own."""
    headers = cod_signed_in(MEMBER_PHONE)

    response = cod_client.post("/v1/me/kyc", json=_submission(), headers=headers)

    assert response.status_code == 201, response.text
    assert (
        sql(
            "SELECT 1 FROM profiles p JOIN users u ON u.id = p.user_id" " WHERE u.phone_e164 = %s",
            (MEMBER_PHONE,),
        )
        == []
    )


# ------------------------------------------------------------------------------- who may submit


def test_authentication_is_required(cod_client: TestClient) -> None:
    """The door is the caller's own account, so a request with no token is refused."""
    response = cod_client.post("/v1/me/kyc", json=_submission())

    assert response.status_code == 401, response.text


def test_a_case_belongs_to_the_caller(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    sql: Callable[..., Rows],
) -> None:
    """The case is written against the caller, never a name in the body.

    There is no field for the account a case is about, which is what makes "submit for someone
    else" impossible rather than merely refused: the identifier is the token.
    """
    member_headers = cod_signed_in(MEMBER_PHONE)
    other_headers = cod_signed_in(OTHER_MEMBER_PHONE)

    assert (
        cod_client.post("/v1/me/kyc", json=_submission(), headers=member_headers).status_code == 201
    )
    assert (
        cod_client.post("/v1/me/kyc", json=_submission(), headers=other_headers).status_code == 201
    )

    assert len(_cases_for(sql, MEMBER_PHONE)) == 1
    assert len(_cases_for(sql, OTHER_MEMBER_PHONE)) == 1
