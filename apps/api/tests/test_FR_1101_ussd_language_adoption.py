"""FR-1101, the language a handset chose is the language its account is born with.

A trader who tells the USSD menu which language they want has made a choice; the channel
remembers it under the number for thirty days, and the first sign-in adopts it. This file
holds that loop across both surfaces: the choice is made on the handset, and the OTP verify
that registers the number stores the same choice on the account - unless the app itself
states a language, in which case the app's newer word wins.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration

PICKER = "+251911000003"


def test_a_language_chosen_on_the_handset_is_adopted_by_first_sign_in(
    client: TestClient,
    ussd_session: Callable[..., httpx.Response],
    sign_in: Callable[..., httpx.Response],
) -> None:
    """The untrained number dials, chooses Oromo, and registers into an Oromo account."""

    opened = ussd_session(client, "", session_id="ses-lang-1", phone_number=PICKER)
    assert opened.status_code == 200
    assert "3. Afaan Oromoo" in opened.text

    chosen = ussd_session(client, "3", session_id="ses-lang-1", phone_number=PICKER)
    assert chosen.text == (
        "CON 1. Gabaa koo\n2. Midhaan ilaaluu\n3. Ajaja koo\n4. Gatii har'aa\n5. Gargaarsa"
    )

    # The verify that creates the account carries no language of its own: the stored choice
    # is what the account stores.
    verified = sign_in(PICKER)
    assert verified.status_code == 200, verified.text
    headers = {"Authorization": f"Bearer {verified.json()['access_token']}"}
    identity = client.get("/v1/me", headers=headers)
    assert identity.status_code == 200, identity.text
    assert identity.json()["language"] == "om"


def test_a_language_the_app_states_overrides_the_stored_choice(
    client: TestClient,
    ussd_session: Callable[..., httpx.Response],
    sign_in: Callable[..., httpx.Response],
) -> None:
    """The newer word wins: what the app names in the request is what the account stores."""

    ussd_session(client, "", session_id="ses-lang-2", phone_number=PICKER)
    ussd_session(client, "2", session_id="ses-lang-2", phone_number=PICKER)  # stored: English

    verified = sign_in(PICKER, language="am")
    assert verified.status_code == 200, verified.text
    headers = {"Authorization": f"Bearer {verified.json()['access_token']}"}
    identity = client.get("/v1/me", headers=headers)
    assert identity.status_code == 200, identity.text
    assert identity.json()["language"] == "am"
