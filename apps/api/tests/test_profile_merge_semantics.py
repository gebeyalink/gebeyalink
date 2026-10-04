"""Merging, validating, and versioning a profile change.

These are the rules around the fields rather than the fields themselves: what a merge does,
what is refused, and what happens to the stored version on each write.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

# SPEC §20: integration. Merge semantics are about what a second write does to a row that
# is already there — the previous version, the untouched fields and the refused write are
# all only observable against a stored row, never from the request alone.
pytestmark = pytest.mark.integration


def test_a_second_write_leaves_the_earlier_fields_alone(
    client: TestClient, signed_in: Callable[..., dict[str, str]]
) -> None:
    """Each write touches only the fields it carries.

    This is the property an offline client depends on: it may hold a stale copy of the rest
    and has no way to learn what we hold, so anything it does not send must survive
    untouched.
    """
    headers = signed_in()
    client.patch(
        "/v1/me",
        headers=headers,
        json={"display_name": "Hana Mekonnen", "region_code": "AA", "kebele": "Bole"},
    )
    client.patch("/v1/me", headers=headers, json={"kebele": "Kirkos"})

    body = client.get("/v1/me", headers=headers).json()
    assert body["kebele"] == "Kirkos"
    assert body["display_name"] == "Hana Mekonnen"
    assert body["region_code"] == "AA"


def test_sending_a_field_as_nothing_leaves_it_as_it_was(
    client: TestClient, signed_in: Callable[..., dict[str, str]]
) -> None:
    """Clearing a field is not something a partial update can be asked to do.

    A request that carries only some fields is a merge, and nothing in the contract gives a
    caller a way to say "forget this one", so a null arriving in a merge is treated as absent
    rather than as an instruction to empty the column.
    """
    headers = signed_in()
    client.patch("/v1/me", headers=headers, json={"kebele": "Bole", "display_name": "Hana M"})
    client.patch("/v1/me", headers=headers, json={"kebele": None})

    assert client.get("/v1/me", headers=headers).json()["kebele"] == "Bole"


def test_the_version_goes_up_by_one_per_write(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    profiles: Callable[[], list[dict[str, Any]]],
) -> None:
    """Each change to the stored profile counts once, however many fields it carried.

    A row starts at zero when it is first written and moves by one on every write after that,
    so a client can tell how many times what it holds has been overtaken. The first write
    creating the row is not counted, because there was nothing there to be out of step with.
    """
    headers = signed_in()
    client.patch("/v1/me", headers=headers, json={"display_name": "Hana Mekonnen"})
    assert profiles()[0]["version"] == 0

    client.patch("/v1/me", headers=headers, json={"region_code": "AA", "kebele": "Bole"})
    assert profiles()[0]["version"] == 1

    client.patch("/v1/me", headers=headers, json={"kebele": "Kirkos"})
    assert profiles()[0]["version"] == 2


def test_a_refused_write_changes_nothing_at_all(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    profiles: Callable[[], list[dict[str, Any]]],
) -> None:
    """Checks run before anything is written, so a bad field cannot half-apply.

    A request carrying one good field and one impossible one is refused whole. Applying the
    good half would leave the trader in a state they never asked for and cannot see.
    """
    headers = signed_in()
    client.patch("/v1/me", headers=headers, json={"display_name": "Hana Mekonnen"})
    before = profiles()[0]

    response = client.patch(
        "/v1/me",
        headers=headers,
        json={"kebele": "Kirkos", "region_code": "ZZ"},
    )

    assert response.status_code == 422
    assert profiles()[0] == before
    assert client.get("/v1/me", headers=headers).json()["kebele"] is None


def test_a_region_that_was_never_seeded_is_refused(
    client: TestClient, signed_in: Callable[..., dict[str, str]]
) -> None:
    """The regions are what the migration seeded, not whatever a client sends.

    Checked against the table so that adding a region is a reviewed migration rather than
    something a caller can talk the platform into believing.
    """
    headers = signed_in()
    response = client.patch("/v1/me", headers=headers, json={"region_code": "ZZ"})

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert "region_code" in error["details"]["fields"]


def test_a_woreda_that_does_not_exist_is_reported_as_missing(
    client: TestClient, signed_in: Callable[..., dict[str, str]]
) -> None:
    """An identifier pointing at nothing is a missing resource, not a malformed field.

    The caller is holding an identifier that has gone away and needs to be told which, so this
    is a NOT_FOUND rather than a validation failure.
    """
    headers = signed_in()
    response = client.patch("/v1/me", headers=headers, json={"woreda_id": 999999})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_a_seeded_woreda_is_accepted(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    profiles: Callable[[], list[dict[str, Any]]],
) -> None:
    """The counterpart: a woreda that exists is written without complaint.

    Read out of the database because a 200 on its own would not show the identifier landed.
    """
    headers = signed_in()
    response = client.patch("/v1/me", headers=headers, json={"woreda_id": 1})

    assert response.status_code == 200, response.text
    assert profiles()[0]["woreda_id"] == 1


def test_a_language_on_its_own_is_worth_a_write_of_its_own(
    client: TestClient, signed_in: Callable[..., dict[str, str]]
) -> None:
    """Changing language alone is a change, and it survives the request that made it.

    Language lives on the account rather than the profile, so this exercises a write that
    touches no profile field at all: the row must not appear, and the choice must still be
    there for the next request.
    """
    headers = signed_in()
    response = client.patch("/v1/me", headers=headers, json={"language": "om"})

    assert response.status_code == 200, response.text
    assert response.json()["language"] == "om"
    assert client.get("/v1/me", headers=headers).json()["language"] == "om"


def test_a_language_sent_with_a_profile_field_keeps_both(
    client: TestClient, signed_in: Callable[..., dict[str, str]]
) -> None:
    """The two live on different tables, and one request can write to both.

    Checked on a later request so that what comes back is what was stored rather than what
    was echoed.
    """
    headers = signed_in()
    client.patch(
        "/v1/me", headers=headers, json={"language": "en", "display_name": "Hana Mekonnen"}
    )

    body = client.get("/v1/me", headers=headers).json()
    assert body["language"] == "en"
    assert body["display_name"] == "Hana Mekonnen"


def test_a_name_that_is_only_spaces_is_not_a_name(
    client: TestClient, signed_in: Callable[..., dict[str, str]]
) -> None:
    """Two characters long and not a name.

    The length rule is satisfied by whitespace alone, so the trimmed form has to be the form
    that was sent before the field is accepted.
    """
    headers = signed_in()
    response = client.patch("/v1/me", headers=headers, json={"display_name": "  "})

    assert response.status_code == 422
    assert "display_name" in response.json()["error"]["details"]["fields"]


def test_a_name_padded_with_spaces_is_refused(
    client: TestClient, signed_in: Callable[..., dict[str, str]]
) -> None:
    """The spaces are kept out rather than quietly trimmed.

    Storing the trimmed version would mean the response did not match what was sent, and a
    client comparing the two would be told the write had failed.
    """
    headers = signed_in()
    response = client.patch("/v1/me", headers=headers, json={"display_name": "  Hana  "})

    assert response.status_code == 422


def test_a_name_at_the_edge_of_the_length_rule_is_accepted(
    client: TestClient, signed_in: Callable[..., dict[str, str]]
) -> None:
    """Two characters is the shortest allowed and sixty the longest, and both work.

    Both ends are checked because a rule that only ever tested the middle would let one of
    them through unnoticed.
    """
    headers = signed_in()
    assert client.patch("/v1/me", headers=headers, json={"display_name": "Ab"}).status_code == 200

    sixty = "A" * 60
    assert client.patch("/v1/me", headers=headers, json={"display_name": sixty}).status_code == 200
    assert client.get("/v1/me", headers=headers).json()["display_name"] == sixty


def test_a_name_beyond_the_length_rule_is_refused(
    client: TestClient, signed_in: Callable[..., dict[str, str]]
) -> None:
    """One character too long is still refused, at the edge as well as well beyond it."""
    headers = signed_in()

    assert client.patch("/v1/me", headers=headers, json={"display_name": "A"}).status_code == 422
    assert (
        client.patch("/v1/me", headers=headers, json={"display_name": "A" * 61}).status_code == 422
    )


def test_a_kebele_beyond_the_column_is_refused(
    client: TestClient, signed_in: Callable[..., dict[str, str]]
) -> None:
    """Eighty characters is what the column holds, and the request is held to it.

    Refused at the request rather than left to the database, so the trader is told what was
    wrong instead of receiving a server error.
    """
    headers = signed_in()
    response = client.patch("/v1/me", headers=headers, json={"kebele": "B" * 81})

    assert response.status_code == 422


def test_a_request_with_nothing_to_say_is_accepted_and_changes_nothing(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    profiles: Callable[[], list[dict[str, Any]]],
) -> None:
    """An empty patch is harmless.

    A client may send one without knowing it has anything to record, and it should get a
    truthful answer back rather than an error or a half-written row.
    """
    headers = signed_in()
    client.patch("/v1/me", headers=headers, json={"display_name": "Hana Mekonnen"})

    response = client.patch("/v1/me", headers=headers, json={})

    assert response.status_code == 200, response.text
    assert response.json()["display_name"] == "Hana Mekonnen"
    # A write that changed nothing leaves the version alone: no client is out of step with
    # what we hold as a result of a request that stored nothing.
    assert profiles()[0]["version"] == 0


def test_an_empty_patch_before_any_write_creates_no_row(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    profiles: Callable[[], list[dict[str, Any]]],
) -> None:
    """A profile row appears when there is something to record, not when it is asked about.

    An empty row would be a thing to keep in step with the account for no gain.
    """
    headers = signed_in()
    response = client.patch("/v1/me", headers=headers, json={})

    assert response.status_code == 200, response.text
    assert profiles() == []


def test_a_field_the_contract_does_not_have_is_refused(
    client: TestClient, signed_in: Callable[..., dict[str, str]]
) -> None:
    """An unexpected field is a mistake worth reporting, not one to keep quietly.

    Otherwise a caller who misspells a field is told everything succeeded while their change
    went nowhere.
    """
    headers = signed_in()
    response = client.patch("/v1/me", headers=headers, json={"disply_name": "Hana"})

    assert response.status_code == 422


def test_a_tier_cannot_be_set_by_a_caller(
    client: TestClient, signed_in: Callable[..., dict[str, str]]
) -> None:
    """Verification level is the platform's to decide, and no request may set it."""
    headers = signed_in()
    response = client.patch("/v1/me", headers=headers, json={"kyc_tier": "TIER_2"})

    assert response.status_code == 422


def test_one_traders_write_does_not_reach_another(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    profiles: Callable[[], list[dict[str, Any]]],
) -> None:
    """Two accounts hold two profiles, and neither write touches the other's."""
    first = signed_in("+251911000001")
    second = signed_in("+251911000002")

    client.patch("/v1/me", headers=first, json={"display_name": "Hana Mekonnen"})
    client.patch("/v1/me", headers=second, json={"display_name": "Selam Girma"})

    stored = profiles()
    assert len(stored) == 2
    assert {row["display_name"] for row in stored} == {"Hana Mekonnen", "Selam Girma"}
