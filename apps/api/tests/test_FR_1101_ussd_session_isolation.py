"""FR-1101, session isolation: a session is the property of the number that dialled it.

The gateway reuses session ids across handsets the way call networks do, so nothing here may
bleed: two sessions on the same network must advance independently, and a handset that posts
on a session id it never dialled must be shown the top of its own menu, not the stranger's.
The engine pins the dialler's number to the session, and this file proves both halves of that.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration

PHONE_A = "+251911000001"
PHONE_B = "+251911000003"
STRANGER = "+251911000099"


def test_two_sessions_same_network_advance_independently(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    ussd_session: Callable[..., httpx.Response],
) -> None:
    """Whatever one handset chooses is stored under its own session, not shared."""

    signed_in(PHONE_A, language="en")
    signed_in(PHONE_B, language="en")

    first_a = ussd_session(client, "", session_id="ses-iso-a", phone_number=PHONE_A)
    first_b = ussd_session(client, "", session_id="ses-iso-b", phone_number=PHONE_B)
    assert first_a.text == first_b.text

    # A and B go down different paths of the same size.
    path_a = ussd_session(client, "2", session_id="ses-iso-a", phone_number=PHONE_A)
    path_b = ussd_session(client, "3", session_id="ses-iso-b", phone_number=PHONE_B)
    assert path_a.text.startswith("CON Last used:")
    assert "Browse Crops" not in path_b.text
    assert path_b.text == "CON 1. Awaiting funding\n2. In progress\n3. Completed"

    # Each session still answers for its own dialler afterwards.
    deeper_a = ussd_session(client, "8", session_id="ses-iso-a", phone_number=PHONE_A)
    assert deeper_a.text.startswith("CON Choose a crop:")


def test_a_session_id_is_answered_by_the_number_that_dialled_it(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    ussd_session: Callable[..., httpx.Response],
) -> None:
    """A handset posting on a stranger's session starts its own, and is never shown theirs."""

    signed_in(PHONE_A, language="en")
    opened = ussd_session(client, "", session_id="ses-iso-shared", phone_number=PHONE_A)
    assert opened.text.startswith("CON 1. My Listings")
    moved = ussd_session(client, "2", session_id="ses-iso-shared", phone_number=PHONE_A)
    assert moved.text.startswith("CON Last used:")

    # A stranger borrows the same session id: they get the fresh-dial opening, not A's screen.
    stranger = ussd_session(client, "2", session_id="ses-iso-shared", phone_number=STRANGER)
    assert stranger.text.startswith("CON ")
    assert "Last used:" not in stranger.text
    assert "Afaan Oromoo" in stranger.text  # the language menu
    assert "1. My Listings" not in stranger.text

    # And A, back on the id the stranger now occupies, is likewise not shown the stranger's.
    again = ussd_session(client, "3", session_id="ses-iso-shared", phone_number=PHONE_A)
    assert again.text.startswith("CON 1. My Listings")
