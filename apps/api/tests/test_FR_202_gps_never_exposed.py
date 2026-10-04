"""A trader's location is taken, and then kept to themselves.

These tests exist because the guarantee is easy to lose by accident. A field that is
accepted on write and absent from the schema has a habit of turning up in a response the
first time somebody adds a convenience serialiser, and a point that has been written cannot
be taken back out of the database. So the tests check both halves: it is genuinely stored,
and it is genuinely never returned.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi.testclient import TestClient

#: Somewhere real, so the value is not obviously a placeholder.
ADDIS_ABABA = (9.0192, 38.7525)


def test_a_coordinate_sent_is_actually_stored(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    profiles: Callable[[], list[dict[str, Any]]],
) -> None:
    """Accepting a point and then throwing it away would be worse than refusing it.

    Read out of the geography column itself, which is the only place this could have ended
    up, and checked for both the coordinate order and the declared projection.
    """
    headers = signed_in()
    response = client.patch(
        "/v1/me",
        headers=headers,
        json={"gps": {"latitude": ADDIS_ABABA[0], "longitude": ADDIS_ABABA[1]}},
    )
    assert response.status_code == 200, response.text

    stored = profiles()[0]
    # PostGIS writes a point as POINT(longitude latitude), so a stored value reading
    # "38.7525 9.0192" is the pair sent the right way round.
    assert stored["gps_text"] == "POINT(38.7525 9.0192)"
    assert stored["gps_srid"] == 4326


def test_the_point_is_stored_at_about_a_metre(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    profiles: Callable[[], list[dict[str, Any]]],
) -> None:
    """Five decimal places, and not more.

    A phone cannot place somebody more finely than this, so the extra digits a caller sends
    would be noise stored as if it were a position. Checking the stored value rather than the
    response, since the response carries no coordinates at all.
    """
    headers = signed_in()
    client.patch(
        "/v1/me",
        headers=headers,
        json={"gps": {"latitude": 9.01923456789, "longitude": 38.75254321098}},
    )

    assert profiles()[0]["gps_text"] == "POINT(38.75254 9.01923)"


def test_no_response_ever_carries_the_point(
    client: TestClient, signed_in: Callable[..., dict[str, str]]
) -> None:
    """Not the write that stored it, and not any read afterwards.

    Two requests because the first is the one that could plausibly echo what it was given,
    and the second is the one a client would cache and show to somebody else.
    """
    headers = signed_in()
    sent = {"gps": {"latitude": ADDIS_ABABA[0], "longitude": ADDIS_ABABA[1]}}

    written = client.patch("/v1/me", headers=headers, json=sent)
    read = client.get("/v1/me", headers=headers)

    for response in (written, read):
        body = response.text
        assert response.status_code == 200, body
        assert "gps" not in response.json()
        # The numbers themselves, not just the key: a serialiser that flattened the point
        # into loose numbers would have left no "gps" to find.
        assert "9.0192" not in body
        assert "38.7525" not in body


def test_the_point_does_not_leak_through_an_error(
    client: TestClient, signed_in: Callable[..., dict[str, str]]
) -> None:
    """A refused change says what was wrong, not what else was sent.

    An error body is the easiest place for a whole request to be echoed back by accident, so
    this sends a good point alongside a field that fails and checks the point is absent from
    the failure.
    """
    headers = signed_in()
    client.patch(
        "/v1/me",
        headers=headers,
        json={
            "gps": {"latitude": ADDIS_ABABA[0], "longitude": ADDIS_ABABA[1]},
            "display_name": "x",
        },
    )

    response = client.patch(
        "/v1/me",
        headers=headers,
        json={
            "gps": {"latitude": ADDIS_ABABA[0], "longitude": ADDIS_ABABA[1]},
            "display_name": "x",
        },
    )

    assert response.status_code == 422
    assert "9.0192" not in response.text
    assert "38.7525" not in response.text
    assert "gps" not in response.text


def test_a_point_outside_the_world_is_refused(
    client: TestClient, signed_in: Callable[..., dict[str, str]]
) -> None:
    """A latitude beyond the poles is not a place, and is not silently clamped.

    Checked at the edge of the request, so an impossible coordinate cannot reach the
    database at all.
    """
    headers = signed_in()
    response = client.patch(
        "/v1/me", headers=headers, json={"gps": {"latitude": 91.0, "longitude": 38.75}}
    )

    assert response.status_code == 422
    assert "VALIDATION_ERROR" in response.text


def test_a_point_can_be_replaced_without_disturbing_the_rest(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    profiles: Callable[[], list[dict[str, Any]]],
) -> None:
    """Moving a trader changes where they are and nothing else.

    The second write replaces the stored point; the name and kebele set earlier are still
    there afterwards, which is what makes this a merge rather than a replacement.
    """
    headers = signed_in()
    client.patch(
        "/v1/me",
        headers=headers,
        json={
            "display_name": "Selamawit Girma",
            "kebele": "Kirkos",
            "gps": {"latitude": ADDIS_ABABA[0], "longitude": ADDIS_ABABA[1]},
        },
    )
    client.patch("/v1/me", headers=headers, json={"gps": {"latitude": 9.032, "longitude": 38.746}})

    stored = profiles()[0]
    assert stored["gps_text"] == "POINT(38.746 9.032)"
    assert stored["display_name"] == "Selamawit Girma"
    assert stored["kebele"] == "Kirkos"


def test_no_point_at_all_is_a_normal_thing_to_have(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    profiles: Callable[[], list[dict[str, Any]]],
) -> None:
    """A trader is not made to give a location to use the platform.

    Absent and refused are different outcomes, and only one of them is acceptable here.
    """
    headers = signed_in()
    response = client.patch("/v1/me", headers=headers, json={"display_name": "Marta Tesfaye"})

    assert response.status_code == 200, response.text
    assert profiles()[0]["gps_text"] is None
