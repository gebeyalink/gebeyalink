"""What a listing is made of, and which of those things it may not be.

The fields themselves and the rules about their values are both here, because for a listing
they are one thing: a row of numbers that mean nothing together is not a listing anybody can
act on. A quantity no larger than the smallest order, a delivery the seller promises to
carry for nothing, a harvest last month — each is a field that was accepted and a promise
that could not be kept.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from httpx import Response

# The agent fixture chain is imported from the file that first needed it: `agent_headers`
# reads the account's tier through `sql` and is approved through `approve`, whose own
# dependency is `run_command`, so pytest has to see all four names here.
from apps.api.tests.test_FR_1201_1203_1707_agent_basics import (  # noqa: F401
    agent_headers,
    approve,
    run_command,
    sql,
)

# SPEC §20: this file drives the real app against an ephemeral Postgres through the client
# fixture, so it is an integration-layer test.
pytestmark = pytest.mark.integration


def test_fr_302_all_channels_write_source_and_honour_constraints(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    stored_listings: Callable[[], list[dict[str, object]]],
    stored_photos: Callable[[], list[dict[str, object]]],
    code_for: Callable[[str], str],
    ussd_session: Callable[..., Response],
    sms_inbound: Callable[..., Response],
    # The four names below are the imported pytest fixtures; the parameter is how pytest is
    # asked for one, not a redefinition of the module-level import.
    agent_headers: dict[str, str],  # noqa: F811
    sql: Callable[..., list[dict[str, object]]],  # noqa: F811
) -> None:
    """FR-302's matrix: each channel writes its own `source`, and the rule that channel
    carries answers for the row it writes.

    All four run through one application — the COD-enabled build, because that is the build
    the agent channel serves and an approved agent is what makes an `AGENT` row possible. A
    test may not drive two of these clients at once, so there is one client here and every
    sign-in goes through its own `cod_signed_in`. The listings are read back from PostgreSQL
    rather than from the responses, so what is checked is what was stored.
    """

    from apps.api.tests.test_FR_1101_ussd_create_listing import SELLER as USSD_SELLER
    from apps.api.tests.test_FR_1101_ussd_create_listing import _ready, _walk
    from apps.api.tests.test_FR_1102_sms_inbound_commands import SELLER as SMS_SELLER
    from apps.api.tests.test_FR_1102_sms_inbound_commands import _as_seller
    from apps.api.tests.test_FR_1201_1203_1707_agent_basics import FARMER_PHONE, _onboard

    # APP: the seller posts the body directly, and the photographs come with it.
    before_app = len(stored_listings())
    posted = cod_client.post(
        "/v1/listings",
        headers=cod_signed_in(),
        json=listing_body(
            photos=[
                {"url": "https://cdn.gebeyalink.et/teff-app.jpg", "width": 1200, "height": 1200}
            ]
        ),
    )
    assert posted.status_code == 201, posted.text
    app_rows = stored_listings()[before_app:]
    assert len(app_rows) == 1
    assert app_rows[0]["source"] == "APP"
    app_photos = [
        photo for photo in stored_photos() if str(photo["listing_id"]) == str(app_rows[0]["id"])
    ]
    assert len(app_photos) == 1
    assert app_photos[0]["url"] == "https://cdn.gebeyalink.et/teff-app.jpg"

    # USSD: the wizard's seven answers, and no screen that would attach a photograph.
    before_ussd = len(stored_listings())
    _ready(cod_client, cod_signed_in, USSD_SELLER)
    ussd_replies = _walk(
        cod_client,
        ussd_session,
        ["1", "1", "1", "500", "2500", "1", "1"],
        session_id="ses-fr302-ussd",
        phone_number=USSD_SELLER,
    )
    # The confirmation is rendered in the account's own language, which the COD sign-in
    # leaves at the platform default; the wizard is the same seven keys either way. What is
    # claimed is the row below, not the wording of the screen.
    assert ussd_replies[-1].text.startswith("END "), ussd_replies[-1].text
    ussd_rows = stored_listings()[before_ussd:]
    assert len(ussd_rows) == 1
    assert ussd_rows[0]["source"] == "USSD"
    assert ussd_rows[0]["quantity_kg"] == 500
    ussd_photos = [
        photo for photo in stored_photos() if str(photo["listing_id"]) == str(ussd_rows[0]["id"])
    ]
    assert ussd_photos == []

    # SMS: a POST carries only a crop, a weight and a price, so §14.4's defaults answer for
    # the rest of the row: no minimum order, pickup only, ready now.
    before_sms = len(stored_listings())
    _as_seller(cod_client, cod_signed_in)
    sms_response = sms_inbound(cod_client, "POST teff 1500 30", from_phone=SMS_SELLER)
    assert sms_response.status_code == 200, sms_response.text
    sms_rows = stored_listings()[before_sms:]
    assert len(sms_rows) == 1
    assert sms_rows[0]["source"] == "SMS"
    assert sms_rows[0]["quantity_kg"] == 1500
    assert sms_rows[0]["min_order_kg"] == 1
    assert "PICKUP" in str(sms_rows[0]["delivery_options"])
    assert sms_rows[0]["harvest_date"] is not None

    # AGENT: an approved agent posts for a farmer it has just taken on. The row belongs to the
    # farmer, and `on_behalf_of_user_id` is the only place the delegation is written.
    before_agent = len(stored_listings())
    confirmed = _onboard(cod_client, agent_headers, code_for, FARMER_PHONE)
    farmer_id = str(confirmed.json()["id"])
    posted_on_behalf = cod_client.post(
        "/v1/listings",
        headers=agent_headers,
        json=listing_body(on_behalf_of_user_id=farmer_id),
    )
    assert posted_on_behalf.status_code == 201, posted_on_behalf.text
    agent_rows = stored_listings()[before_agent:]
    assert len(agent_rows) == 1
    assert agent_rows[0]["source"] == "AGENT"
    assert str(agent_rows[0]["seller_id"]) == farmer_id
    delegated = sql(
        "SELECT on_behalf_of_user_id FROM listings WHERE id = %s",
        (agent_rows[0]["id"],),
    )
    assert str(delegated[0]["on_behalf_of_user_id"]) == farmer_id
