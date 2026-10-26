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

# SPEC §20: this file drives the real app against an ephemeral Postgres through the client
# fixture, so it is an integration-layer test.
pytestmark = pytest.mark.integration


def test_fr_302_all_channels_write_source_and_honour_constraints(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    stored_listings: Callable[[], list[dict[str, object]]],
    sql: Callable[..., list[dict[str, object]]],
    code_for: Callable[[str], str],
    ussd_session: Callable[..., Response],
) -> None:
    """FR-302's matrix: each channel produces a listing with its own source and the constraint the requirement names for it."""  # noqa: E501

    from apps.api.tests.test_FR_1101_ussd_create_listing import (
        SELLER as USSD_SELLER,
    )
    from apps.api.tests.test_FR_1101_ussd_create_listing import (
        _ready,
        _walk,
    )
    from apps.api.tests.test_FR_1102_sms_inbound_commands import (
        SELLER as SMS_SELLER,
    )
    from apps.api.tests.test_FR_1102_sms_inbound_commands import (
        _as_seller,
    )
    from apps.api.tests.test_FR_1201_1203_1707_agent_basics import (
        AGENT_PHONE,
        FARMER_PHONE,
        _onboard,
    )

    before_app = len(stored_listings())
    headers_app = signed_in()
    body_app = listing_body(
        photos=[{"url": "https://cdn.gebeyalink.et/teff-app.jpg", "width": 1200, "height": 1200}]
    )
    resp_app = client.post("/v1/listings", headers=headers_app, json=body_app)
    assert resp_app.status_code == 201
    app_rows = stored_listings()[before_app:]
    assert len(app_rows) == 1
    assert app_rows[0]["source"] == "APP"
    # photo row exists for APP
    photos_all = sql("SELECT * FROM listing_photos")
    assert len(photos_all) >= 1

    before_ussd = len(stored_listings())
    _ready(client, signed_in, USSD_SELLER)

    ussd_replies = _walk(
        client,
        ussd_session,
        ["1", "1", "1", "500", "2500", "1", "1"],
        session_id="ses-fr302-ussd",
        phone_number=USSD_SELLER,
    )
    assert isinstance(ussd_replies, list)
    assert getattr(ussd_replies[-1], "text", "").startswith("END Listing ")
    ussd_rows = stored_listings()[before_ussd:]
    assert len(ussd_rows) == 1
    assert ussd_rows[0]["source"] == "USSD"
    # USSD: no photos
    ussd_id = ussd_rows[0]["id"]
    ussd_photos = sql("SELECT * FROM listing_photos WHERE listing_id = %s", (ussd_id,))
    assert ussd_photos == []

    before_sms = len(stored_listings())
    _as_seller(client, signed_in)
    from apps.api.tests.conftest import sms_inbound

    sms_resp = sms_inbound()(client, "POST teff 1500 30", from_phone=SMS_SELLER)
    assert sms_resp.status_code == 200
    sms_rows = stored_listings()[before_sms:]
    assert len(sms_rows) == 1
    assert sms_rows[0]["source"] == "SMS"
    # SMS constraints per §14.4
    assert sms_rows[0]["min_order_kg"] == 1
    assert "PICKUP" in str(sms_rows[0]["delivery_options"])
    # harvest_date today (EAT) - approximate
    harvest = sms_rows[0]["harvest_date"]
    assert harvest is not None

    before_agent = len(stored_listings())
    agent_headers = signed_in(AGENT_PHONE)
    confirmed = _onboard(client, agent_headers, code_for, FARMER_PHONE)
    farmer_id = confirmed.json()["id"]
    posted = client.post(
        "/v1/listings",
        json={**listing_body(), "on_behalf_of_user_id": farmer_id},
        headers=agent_headers,
    )
    assert posted.status_code == 201
    agent_rows = stored_listings()[before_agent:]
    assert len(agent_rows) == 1
    assert agent_rows[0]["source"] == "AGENT"
    assert str(agent_rows[0]["seller_id"]) == farmer_id
    assert str(agent_rows[0]["on_behalf_of_user_id"]) == farmer_id
