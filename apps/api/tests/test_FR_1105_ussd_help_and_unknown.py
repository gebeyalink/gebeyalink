"""FR-1105, the help screen, and everything the menus mean by being finite.

The fifth line of the main menu is the human help route: a number to call and the word to
text. It is a terminal screen - there is nothing to branch from it - and the help it names
is the same short code the SMS channel answers. The rest of the file is the boundary of the
menus themselves: a press the menu does not name is refused on the same screen with the
same marker, three times, and the fourth time the session parkes on the help screen.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration

DIALLER = "+251911000099"
SESSION_ID = "ses-hlp-1"

HELP = "END Call 951 free / SMS HELP. Agents in your kebele can assist."


def _open_main_menu(client: TestClient, ussd_session: Callable[..., httpx.Response]) -> None:
    """An untrained number picks English and lands on the main menu."""
    opened = ussd_session(client, "", session_id=SESSION_ID, phone_number=DIALLER)
    assert opened.status_code == 200
    assert opened.text.startswith("CON ገበያሊንክ እንኳን ደህና መጡ\n")
    assert "3. Afaan Oromoo" in opened.text
    main = ussd_session(client, "2", session_id=SESSION_ID, phone_number=DIALLER)
    assert main.text == (
        "CON 1. My Listings\n2. Browse Crops\n3. My Orders\n4. Today's Prices\n5. Help"
    )


def test_help_is_the_terminal_fifth_line(
    client: TestClient,
    ussd_session: Callable[..., httpx.Response],
) -> None:
    _open_main_menu(client, ussd_session)
    help_screen = ussd_session(client, "5", session_id=SESSION_ID, phone_number=DIALLER)
    assert help_screen.text == HELP


def test_an_unnamed_press_refuses_on_the_same_screen(
    client: TestClient,
    ussd_session: Callable[..., httpx.Response],
) -> None:
    _open_main_menu(client, ussd_session)
    refused = ussd_session(client, "9", session_id=SESSION_ID, phone_number=DIALLER)
    assert refused.text == (
        "CON ❌ Please choose one of the options.\n"
        "1. My Listings\n2. Browse Crops\n3. My Orders\n4. Today's Prices\n5. Help"
    )


def test_fourth_invalid_press_parks_the_session_on_help(
    client: TestClient,
    ussd_session: Callable[..., httpx.Response],
) -> None:
    """Three refusals keep the menu; the fourth press ends the session on the help screen."""

    _open_main_menu(client, ussd_session)
    for _ in range(3):
        refused = ussd_session(client, "9", session_id=SESSION_ID, phone_number=DIALLER)
        assert refused.text.startswith("CON ❌ Please choose one of the options.")
    parked = ussd_session(client, "9", session_id=SESSION_ID, phone_number=DIALLER)
    assert parked.text == HELP
