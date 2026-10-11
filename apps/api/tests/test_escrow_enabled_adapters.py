"""The feature-phone adapters, sending the payment mode a handset cannot ask for.

The app lets a buyer choose how a sale would be paid for. Neither feature-phone channel has
anywhere to put that choice: the SMS grammar's ``OFFER`` is three tokens and the USSD offer tree
asks for a weight and a price. So when escrow is off — the Phase 1 deployment, which funds only
cash-on-delivery — the adapters send ``COD`` to the domain on the trader's behalf, and when escrow
is on they send nothing and the offer's own default applies.

This file holds two things apart on purpose. The first is the adapters: an SMS ``OFFER`` and a
walked USSD offer each write a row whose ``payment_mode`` is read back from PostgreSQL, not from
the reply. The second is the boundary: the adapter is a convenience, not the enforcement point.
The domain refuses a mode the deployment does not fund whatever channel it arrived on, and the
accept path reads the mode the offer already carries rather than taking one from the sender.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from urllib.parse import urlencode

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from apps.api.core.config import Settings
from apps.api.models.order import PaymentMode
from apps.api.providers.sms import RecordingSmsProvider
from apps.api.services.adapter_offer_mode import adapter_offer_payment_mode
from packages.shared.messaging.signature import sign_gateway_body

SELLER = "+251911000001"
BUYER = "+251911000003"

SESSION_ID = "ses-mode-1"

COUNTER_KG = 500
COUNTER_PRICE_SANTIM = 3_300_000


def _inert_settings(*, escrow_enabled: bool) -> Settings:
    """Configuration pointing nowhere, for the one test that starts nothing.

    The helper reads a flag and returns a value and touches no service, so the unit test that
    holds it passes a settings object rather than booting an application to obtain one.
    """
    return Settings(
        database_url="postgresql+psycopg://nobody@127.0.0.1:1/nothing",
        redis_url="redis://127.0.0.1:1/0",
        signing_private_key=SecretStr("unused-outside-the-application"),
        signing_public_key=SecretStr("unused-outside-the-application"),
        signing_key_id="test-key",
        gateway_secret=SecretStr("test-gateway-secret"),
        terms_version="2026-01-01",
        escrow_enabled=escrow_enabled,
    )


@pytest.mark.unit
def test_the_adapter_sends_cod_only_when_escrow_is_off() -> None:
    """The one condition both adapters share, written once.

    With escrow off, cash-on-delivery is the only mode the deployment funds, so the adapter sends
    it. With escrow on, both modes exist and the choice belongs to the trader, so the adapter
    sends nothing and the offer's own default applies. If this read were wrong the two channels
    would fail differently from each other, which is exactly what one helper exists to prevent.
    """
    assert adapter_offer_payment_mode(_inert_settings(escrow_enabled=False)) is PaymentMode.COD
    assert adapter_offer_payment_mode(_inert_settings(escrow_enabled=True)) is None


def _as_seller(client: TestClient, signed_in: Callable[..., dict[str, str]]) -> dict[str, str]:
    """A seller signed in and given the pilot region, so a listing can be posted."""
    headers = signed_in(SELLER)
    updated = client.patch(
        "/v1/me",
        headers=headers,
        json={"region_code": "OR", "woreda_id": 1, "kebele": "Bole"},
    )
    assert updated.status_code == 200, updated.text
    return headers


def _as_buyer(
    client: TestClient, signed_in_as_buyer: Callable[..., dict[str, str]]
) -> dict[str, str]:
    """A buyer signed in and given the pilot region, so an order could name an address."""
    headers = signed_in_as_buyer(BUYER)
    updated = client.patch(
        "/v1/me",
        headers=headers,
        json={"region_code": "OR", "woreda_id": 1, "kebele": "Bole"},
    )
    assert updated.status_code == 200, updated.text
    return headers


def _post_listing(
    client: TestClient,
    seller: dict[str, str],
    listing_body: Callable[..., dict[str, object]],
    **overrides: object,
) -> dict[str, object]:
    response = client.post("/v1/listings", headers=seller, json=listing_body(**overrides))
    assert response.status_code == 201, response.text
    body: dict[str, object] = response.json()
    return body


def _place_offer(
    client: TestClient,
    buyer: dict[str, str],
    listing_id: object,
    offer_body: Callable[..., dict[str, object]],
    **overrides: object,
) -> httpx.Response:
    response = client.post(
        f"/v1/listings/{listing_id}/offers", headers=buyer, json=offer_body(**overrides)
    )
    assert isinstance(response, httpx.Response)
    return response


def _walk_to_the_offer(
    client: TestClient,
    ussd_session: Callable[..., httpx.Response],
    *,
    phone_number: str = BUYER,
) -> httpx.Response:
    """Press through the browse path to the confirm step and take it."""
    for keypress in ["", "2", "8", "1", "1", "1", "500", "30"]:
        ussd_session(client, keypress, session_id=SESSION_ID, phone_number=phone_number)
    sent: httpx.Response = ussd_session(
        client, "1", session_id=SESSION_ID, phone_number=phone_number
    )
    return sent


def _counter(
    client: TestClient,
    seller: dict[str, str],
    offer_id: object,
    offer_body: Callable[..., dict[str, object]],
) -> dict[str, object]:
    response = client.post(
        f"/v1/offers/{offer_id}/counter",
        headers=seller,
        json=offer_body(quantity_kg=COUNTER_KG, price_santim=COUNTER_PRICE_SANTIM),
    )
    assert response.status_code == 200, response.text
    body: dict[str, object] = response.json()
    return body


@pytest.mark.integration
def test_an_sms_offer_under_cod_sends_cod(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    cod_signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    sms_inbound: Callable[..., httpx.Response],
    stored_offers: Callable[[], list[dict[str, object]]],
) -> None:
    """A texted ``OFFER`` reaches the domain carrying cash-on-delivery.

    The grammar has no fourth token for a mode, so the trader cannot send one; under a deployment
    that funds only COD, an offer with no mode would default to escrow and be refused. The row is
    read from the database, because the reply says the bid was sent either way would not settle
    which mode it carried.
    """
    seller = _as_seller(cod_client, cod_signed_in)
    _as_buyer(cod_client, cod_signed_in_as_buyer)
    listing = _post_listing(cod_client, seller, listing_body)

    response = sms_inbound(cod_client, f"OFFER {listing['short_code']} 500 33", from_phone=BUYER)
    assert response.status_code == 200

    offers = stored_offers()
    assert len(offers) == 1
    assert offers[0]["payment_mode"] == "COD"


@pytest.mark.integration
def test_a_ussd_offer_under_cod_sends_cod(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    cod_signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    ussd_session: Callable[..., httpx.Response],
    stored_offers: Callable[[], list[dict[str, object]]],
) -> None:
    """A dialled offer reaches the domain carrying cash-on-delivery.

    The tree asks for a weight and a price and has no screen for a mode, so the adapter sends it.
    Read from the row, for the same reason the SMS one is.
    """
    seller = _as_seller(cod_client, cod_signed_in)
    _post_listing(cod_client, seller, listing_body, delivery_options=["PICKUP"])
    _as_buyer(cod_client, cod_signed_in_as_buyer)

    sent = _walk_to_the_offer(cod_client, ussd_session)
    assert sent.text.startswith("END ")

    offers = stored_offers()
    assert len(offers) == 1
    assert offers[0]["payment_mode"] == "COD"


@pytest.mark.integration
def test_an_sms_offer_under_escrow_sends_the_default(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    sms_inbound: Callable[..., httpx.Response],
    stored_offers: Callable[[], list[dict[str, object]]],
) -> None:
    """With escrow on, a texted ``OFFER`` sends nothing and takes the offer's default.

    Both modes are available here, so the adapter must not force the trader's hand: the offer
    defaults to escrow exactly as one placed from the app with no mode does.
    """
    seller = _as_seller(client, signed_in)
    _as_buyer(client, signed_in_as_buyer)
    listing = _post_listing(client, seller, listing_body)

    response = sms_inbound(client, f"OFFER {listing['short_code']} 500 33", from_phone=BUYER)
    assert response.status_code == 200

    offers = stored_offers()
    assert len(offers) == 1
    assert offers[0]["payment_mode"] == "ESCROW"


@pytest.mark.integration
def test_a_ussd_offer_under_escrow_sends_the_default(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    ussd_session: Callable[..., httpx.Response],
    stored_offers: Callable[[], list[dict[str, object]]],
) -> None:
    """With escrow on, a dialled offer sends nothing and takes the offer's default."""
    seller = _as_seller(client, signed_in)
    _post_listing(client, seller, listing_body, delivery_options=["PICKUP"])
    _as_buyer(client, signed_in_as_buyer)

    sent = _walk_to_the_offer(client, ussd_session)
    assert sent.text.startswith("END ")

    offers = stored_offers()
    assert len(offers) == 1
    assert offers[0]["payment_mode"] == "ESCROW"


@pytest.mark.integration
def test_an_sms_yes_accept_on_a_cod_offer_writes_a_cod_order(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    cod_signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    issue_action_token: Callable[..., str],
    sms_inbound: Callable[..., httpx.Response],
    stored_offers: Callable[[], list[dict[str, object]]],
    stored_orders: Callable[[], list[dict[str, object]]],
) -> None:
    """``YES`` reads the mode the offer carries; the sender sends none of their own.

    The accept path is unchanged by this slice: the buyer's answer names no mode, and the order
    inherits the cash-on-delivery the countered offer carried. Read from the order row.
    """
    seller = _as_seller(cod_client, cod_signed_in)
    buyer = _as_buyer(cod_client, cod_signed_in_as_buyer)
    listing = _post_listing(cod_client, seller, listing_body, delivery_options=["PICKUP"])

    opening = _place_offer(cod_client, buyer, listing["id"], offer_body, payment_mode="COD")
    assert opening.status_code == 201, opening.text
    counter = _counter(cod_client, seller, opening.json()["id"], offer_body)
    token = issue_action_token(BUYER, f"offer_answer:{counter['id']}")

    response = sms_inbound(cod_client, f"YES {token}", from_phone=BUYER)
    assert response.status_code == 200

    orders = stored_orders()
    assert len(orders) == 1
    assert str(orders[0]["offer_id"]) == counter["id"]
    assert orders[0]["payment_mode"] == "COD"
    statuses = {str(row["id"]): row["status"] for row in stored_offers()}
    assert statuses[counter["id"]] == "ACCEPTED"


async def _send_sms(
    http: httpx.AsyncClient, secret: str, text: str, *, from_phone: str
) -> httpx.Response:
    """Deliver one signed text message over an asynchronous client, the gateway's way."""
    body = urlencode({"from": from_phone, "text": text}).encode("utf-8")
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "X-AT-Signature": sign_gateway_body(secret, body),
    }
    response: httpx.Response = await http.post("/v1/sms/inbound", content=body, headers=headers)
    return response


@pytest.mark.integration
async def test_an_sms_yes_accept_refuses_a_cod_offer_once_cod_is_off(
    escrow_flag_clients: tuple[httpx.AsyncClient, httpx.AsyncClient],
    sign_in_over: Callable[..., Awaitable[dict[str, str]]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    issue_action_token: Callable[..., str],
    gateway_secret: str,
    postgres_url: str,
    sms_recorder: RecordingSmsProvider,
    stored_offers: Callable[[], list[dict[str, object]]],
    stored_orders: Callable[[], list[dict[str, object]]],
) -> None:
    """The accept-time revalidation, over the SMS path.

    The two clients are one deployment before and after cash-on-delivery was turned off: they
    share the database and the signing key, so the token minted while COD was on is the token the
    second app checks, and the offer is the same row. The accept refuses, the offer stays ``OPEN``,
    and no order is written — a configuration change is not the buyer's mistake. The trader sees
    the channel's generic refusal, which carries the token back.
    """
    funding, unfunded = escrow_flag_clients
    seller_phone = "+251911000011"
    buyer_phone = "+251911000013"

    seller = await sign_in_over(unfunded, seller_phone)
    buyer = await sign_in_over(unfunded, buyer_phone)
    sync_url = postgres_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(sync_url, autocommit=True) as connection:
        connection.execute("UPDATE users SET role = 'BUYER' WHERE phone_e164 = %s", (buyer_phone,))
    for headers in (seller, buyer):
        updated = await unfunded.patch(
            "/v1/me",
            headers=headers,
            json={"region_code": "OR", "woreda_id": 1, "kebele": "Bole"},
        )
        assert updated.status_code == 200, updated.text

    posted = await unfunded.post(
        "/v1/listings", headers=seller, json=listing_body(delivery_options=["PICKUP"])
    )
    assert posted.status_code == 201, posted.text
    listing_id = str(posted.json()["id"])

    placed = await unfunded.post(
        f"/v1/listings/{listing_id}/offers", headers=buyer, json=offer_body(payment_mode="COD")
    )
    assert placed.status_code == 201, placed.text
    opening_id = str(placed.json()["id"])

    countered = await unfunded.post(
        f"/v1/offers/{opening_id}/counter",
        headers=seller,
        json=offer_body(quantity_kg=COUNTER_KG, price_santim=COUNTER_PRICE_SANTIM),
    )
    assert countered.status_code == 200, countered.text
    counter_id = str(countered.json()["id"])

    token = issue_action_token(buyer_phone, f"offer_answer:{counter_id}")

    response = await _send_sms(funding, gateway_secret, f"YES {token}", from_phone=buyer_phone)
    assert response.status_code == 200
    assert response.content == b""
    assert token in sms_recorder.messages_to(buyer_phone)[-1].body

    statuses = {str(row["id"]): row["status"] for row in stored_offers()}
    assert statuses[counter_id] == "OPEN"
    assert stored_orders() == []


@pytest.mark.integration
def test_the_domain_refuses_escrow_under_cod_whatever_the_channel(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    cod_signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    offer_body: Callable[..., dict[str, object]],
    stored_offers: Callable[[], list[dict[str, object]]],
) -> None:
    """A mode this deployment does not fund is refused by the domain, not by the adapter.

    The adapter is convenience: it chooses for a trader who cannot. This bid never went near a
    feature-phone adapter — it comes in over HTTP — and is still refused, which is what says the
    enforcement lives in ``OfferService.place`` and a channel that forgot to send a mode could not
    slip a bid past it.
    """
    seller = _as_seller(cod_client, cod_signed_in)
    listing = _post_listing(cod_client, seller, listing_body)
    buyer = _as_buyer(cod_client, cod_signed_in_as_buyer)

    response = _place_offer(cod_client, buyer, listing["id"], offer_body, payment_mode="ESCROW")

    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["details"] == {"feature": "escrow"}
    assert stored_offers() == []
