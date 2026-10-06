"""What the scale said, captured while it still can change anything.

FR-2507 is delivered-weight recording: the seller records the kilograms the handover actually
delivered, separately from either confirmation, because a scale reading is a fact an order can
know long before its buyer is ready to sign it off. The recording is repeatable — last write
wins, and the version bumps so a later dispute can tell two recordings apart — and bounded to
five percent on top of what was ordered, whole kilograms, never past the schema's ceiling.

The tolerance is the other half, and it is checked at the *buyer's* confirmation: the shortfall
travels to the buyer, so it is the buyer's word that is refused when the recorded weight falls
below the receivable minimum. A two-percent tolerance means a delivery of exactly ninety-eight
percent of the order passes and the kilo below it does not; the refusal names the boundary so a
client can show it without recomputing it. A confirmation with no recorded weight refuses
nothing — the buyer confirms against the only fact there is.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from fastapi.testclient import TestClient

# SPEC A20: this file drives the real app against an ephemeral Postgres through a client
# fixture, so it is an integration-layer test.
pytestmark = pytest.mark.integration

CHEAP_ASK_SANTIM = 1_000
COD_BUY_KG = 200


def post_listing(
    client: TestClient,
    headers: dict[str, str],
    body: Callable[..., dict[str, object]],
    **overrides: object,
) -> str:
    """Post a listing and hand back its identity."""
    response = client.post("/v1/listings", headers=headers, json=body(**overrides))
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def buy_now(
    client: TestClient,
    headers: dict[str, str],
    listing_id: str,
    body: Callable[..., dict[str, object]],
    key: Callable[[], str],
    **overrides: object,
) -> dict[str, object]:
    """Buy now and hand back the created order."""
    response = client.post(
        f"/v1/listings/{listing_id}/buy-now",
        headers={**headers, "Idempotency-Key": key()},
        json=body(**overrides),
    )
    assert response.status_code == 201, response.text
    order: dict[str, object] = response.json()
    return order


def confirm(client: TestClient, headers: dict[str, str], order_id: str) -> dict[str, object]:
    """Confirm an order and hand back the read."""
    response = client.post(f"/v1/orders/{order_id}/confirm", headers=headers)
    assert response.status_code == 200, response.text
    order: dict[str, object] = response.json()
    return order


def attempt(
    client: TestClient,
    headers: dict[str, str],
    path: str,
    key: Callable[[], str],
    **request: object,
) -> httpx.Response:
    """Call a fulfillment route, answering either way - for the tests whose point is the refusal."""
    response = client.post(path, headers={**headers, "Idempotency-Key": key()}, **request)
    assert isinstance(response, httpx.Response)
    return response


def ready(
    client: TestClient, headers: dict[str, str], order_id: str, key: Callable[[], str]
) -> dict[str, object]:
    """Mark an order ready for pickup and hand back the read."""
    response = attempt(client, headers, f"/v1/orders/{order_id}/ready", key)
    assert response.status_code == 200, response.text
    order: dict[str, object] = response.json()
    return order


def shipped(
    client: TestClient, headers: dict[str, str], order_id: str, key: Callable[[], str]
) -> dict[str, object]:
    """Mark an order shipped and hand back the read."""
    response = attempt(client, headers, f"/v1/orders/{order_id}/shipped", key)
    assert response.status_code == 200, response.text
    order: dict[str, object] = response.json()
    return order


def record_weight(
    client: TestClient,
    headers: dict[str, str],
    order_id: str,
    key: Callable[[], str],
    weight: int,
) -> httpx.Response:
    """PUT a delivered weight, answering either way."""
    response = client.put(
        f"/v1/orders/{order_id}/weight",
        headers={**headers, "Idempotency-Key": key()},
        json={"delivered_weight_kg": weight},
    )
    assert isinstance(response, httpx.Response)
    return response


def pickup_order(
    client: TestClient,
    seller: dict[str, str],
    buyer: dict[str, str],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    quantity_kg: int,
    **listing_overrides: object,
) -> tuple[str, str]:
    """Stand up a validated COD pickup order of a chosen weight and hand back its identifiers."""
    listing_id = post_listing(
        client,
        seller,
        listing_body,
        quantity_kg=quantity_kg,
        min_order_kg=quantity_kg,
        unit_price_santim=CHEAP_ASK_SANTIM,
        **listing_overrides,
    )
    order = buy_now(
        client,
        buyer,
        listing_id,
        order_body,
        idempotency_key,
        payment_mode="COD",
        quantity_kg=quantity_kg,
    )
    return str(order["id"]), listing_id


def delivery_order(
    client: TestClient,
    seller: dict[str, str],
    buyer: dict[str, str],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
    quantity_kg: int,
) -> tuple[str, str]:
    """Stand up a validated COD seller-delivery order and hand back its identifiers."""
    listing_id = post_listing(
        client,
        seller,
        listing_body,
        quantity_kg=quantity_kg,
        min_order_kg=quantity_kg,
        unit_price_santim=CHEAP_ASK_SANTIM,
        delivery_options=["SELLER_DELIVERY"],
        delivery_fee_santim=150_000,
    )
    order = buy_now(
        client,
        buyer,
        listing_id,
        order_body,
        idempotency_key,
        payment_mode="COD",
        quantity_kg=quantity_kg,
        delivery_method="SELLER_DELIVERY",
    )
    return str(order["id"]), listing_id


def test_the_recordable_bound_is_five_percent_on_top_rounded_up_to_the_kilo(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    cod_signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
) -> None:
    """Thirty-three kilos ordered records at most thirty-four, and thirty-five is refused.

    The upper bound is the five-percent cushion on what was ordered, whole kilograms (FR-2507,
    ADR-E) — "more than was agreed up to five percent" is the handover breaking the low
    tolerance, not this one. The refusal names exactly what it would accept, so a client can show
    the boundary without recomputing it.
    """
    seller = cod_signed_in()
    buyer = cod_signed_in_as_buyer()
    order_id, _ = pickup_order(
        cod_client, seller, buyer, listing_body, order_body, idempotency_key, 33
    )
    confirm(cod_client, buyer, order_id)
    confirm(cod_client, seller, order_id)
    ready(cod_client, seller, order_id, idempotency_key)

    edge = record_weight(cod_client, seller, order_id, idempotency_key, 34)
    assert edge.status_code == 200, edge.text
    assert edge.json()["delivered_weight_kg"] == 34

    over = record_weight(cod_client, seller, order_id, idempotency_key, 35)
    assert over.status_code == 422, over.text
    error = over.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["details"]["fields"]["delivered_weight_kg"] == "outside_recordable_range"
    assert error["details"]["max_delivered_weight_kg"] == 34


def test_recording_is_last_write_wins_and_each_win_bumps_the_version(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    cod_signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
) -> None:
    """Two recordings mean the later one, and a dispute can tell them apart.

    A scale reading is a fact the order can know twice and mean the later of — the first number
    was what the scale said then, and the seller is not writing a history. But exactly because
    the number can change, it earns the version bump no other fulfillment move earns: the row
    needs to be able to say it was read differently before.
    """
    seller = cod_signed_in()
    buyer = cod_signed_in_as_buyer()
    order_id, _ = pickup_order(
        cod_client, seller, buyer, listing_body, order_body, idempotency_key, COD_BUY_KG
    )
    confirm(cod_client, buyer, order_id)
    confirm(cod_client, seller, order_id)
    ready(cod_client, seller, order_id, idempotency_key)
    original_version = cod_client.get(f"/v1/orders/{order_id}", headers=seller).json()["version"]

    first = record_weight(cod_client, seller, order_id, idempotency_key, 200)
    assert first.status_code == 200, first.text
    second = record_weight(cod_client, seller, order_id, idempotency_key, 190)
    assert second.status_code == 200, second.text

    read = cod_client.get(f"/v1/orders/{order_id}", headers=seller).json()
    assert read["delivered_weight_kg"] == 190
    assert read["version"] == original_version + 2


def test_weight_recording_is_the_seller_s_lathe(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    cod_signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
) -> None:
    """The buyer has no scale on the seller's side of the handover.

    The writing records what was handed over, which is the seller's fact to capture; the buyer's
    side of the same handover is the confirmation that weighs it against the tolerance. The
    refusal names which side the wrong caller tried to stand in for.
    """
    seller = cod_signed_in()
    buyer = cod_signed_in_as_buyer()
    order_id, _ = pickup_order(
        cod_client, seller, buyer, listing_body, order_body, idempotency_key, COD_BUY_KG
    )
    confirm(cod_client, buyer, order_id)
    confirm(cod_client, seller, order_id)
    ready(cod_client, seller, order_id, idempotency_key)

    response = record_weight(cod_client, buyer, order_id, idempotency_key, COD_BUY_KG)

    assert response.status_code == 403, response.text
    assert response.json()["error"]["details"]["reason"] == "not_the_seller"


def test_a_weight_cannot_be_recorded_after_the_handover_was_already_counted(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    cod_signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
) -> None:
    """Past the count, a scale reading changes nothing, so it is refused.

    Recording belongs to the window where the goods could still be moving: a weight read after
    the handover has been counted is a weight read past the point it could change anything, and
    the 409 says the order is `DELIVERED` and closes the door on it.
    """
    seller = cod_signed_in()
    buyer = cod_signed_in_as_buyer()
    order_id, _ = pickup_order(
        cod_client, seller, buyer, listing_body, order_body, idempotency_key, COD_BUY_KG
    )
    confirm(cod_client, buyer, order_id)
    confirm(cod_client, seller, order_id)
    ready(cod_client, seller, order_id, idempotency_key)
    code = cod_client.get(f"/v1/orders/{order_id}", headers=buyer).json()["pickup_code"]
    delivered = attempt(
        cod_client,
        seller,
        f"/v1/orders/{order_id}/confirm-pickup",
        idempotency_key,
        json={"pickup_code": code},
    )
    assert delivered.status_code == 200, delivered.text

    response = record_weight(cod_client, seller, order_id, idempotency_key, COD_BUY_KG)

    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert error["code"] == "ILLEGAL_STATE_TRANSITION"
    assert error["details"]["status"] == "DELIVERED"
    assert error["details"]["requested"] == "delivered_weight_kg"


def test_a_delivery_at_exactly_the_receivable_minimum_is_accepted(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    cod_signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
) -> None:
    """Ninety-eight percent of the order is receivable, by the boundary's own arithmetic (FR-2507).

    A thousand kilos ordered tolerates ninety-eight percent of the order: the recorded weight of
    nine hundred and eighty is the exact boundary of a two-percent tolerance, and the boundary
    belongs to the buyer. The buyer's confirmation passes and the order is `DELIVERED`.
    """
    seller = cod_signed_in()
    buyer = cod_signed_in_as_buyer()
    order_id, _ = delivery_order(
        cod_client, seller, buyer, listing_body, order_body, idempotency_key, 1000
    )
    confirm(cod_client, buyer, order_id)
    confirm(cod_client, seller, order_id)
    shipped(cod_client, seller, order_id, idempotency_key)
    recorded = record_weight(cod_client, seller, order_id, idempotency_key, 980)
    assert recorded.status_code == 200, recorded.text

    response = attempt(
        cod_client, buyer, f"/v1/orders/{order_id}/confirm-delivery", idempotency_key
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "DELIVERED"
    assert response.json()["delivered_weight_kg"] == 980


def test_a_delivery_below_the_tolerance_is_refused_at_the_buyer_s_confirmation(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    cod_signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
) -> None:
    """The kilo below the minimum is the buyer's refusal, and it names the boundary.

    Nine hundred and seventy-nine is one kilo under the receivable minimum of nine hundred and
    eighty, so the buyer's confirmation is refused with `below_shrinkage_tolerance` and the
    arithmetic in the details — the order stays `SHIPPED`, the refusal is not cached against the
    key, and once the seller records a receivable weight the same confirmation passes clean.
    """
    seller = cod_signed_in()
    buyer = cod_signed_in_as_buyer()
    order_id, _ = delivery_order(
        cod_client, seller, buyer, listing_body, order_body, idempotency_key, 1000
    )
    confirm(cod_client, buyer, order_id)
    confirm(cod_client, seller, order_id)
    shipped(cod_client, seller, order_id, idempotency_key)
    record_weight(cod_client, seller, order_id, idempotency_key, 979)

    refused = attempt(cod_client, buyer, f"/v1/orders/{order_id}/confirm-delivery", idempotency_key)

    assert refused.status_code == 422, refused.text
    error = refused.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["details"]["fields"]["delivered_weight_kg"] == "below_shrinkage_tolerance"
    assert error["details"]["ordered_kg"] == 1000
    assert error["details"]["delivered_kg"] == 979
    assert error["details"]["tolerance_pct"] == 2.0
    assert error["details"]["min_acceptable_kg"] == 980

    still_shipping = cod_client.get(f"/v1/orders/{order_id}", headers=buyer).json()
    assert still_shipping["status"] == "SHIPPED"

    record_weight(cod_client, seller, order_id, idempotency_key, 990)
    accepted = attempt(
        cod_client, buyer, f"/v1/orders/{order_id}/confirm-delivery", idempotency_key
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "DELIVERED"


def test_a_buyer_who_recorded_nothing_confirms_against_the_only_fact_there_is(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    cod_signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
) -> None:
    """No recorded weight means no shortfall to weigh it against.

    The tolerance applies to the recorded number and only the recorded number: a buyer who
    confirms without one is confirming the handover happened against the only fact there is,
    rather than against a figure the seller never gave. The order reaches `DELIVERED`.
    """
    seller = cod_signed_in()
    buyer = cod_signed_in_as_buyer()
    order_id, _ = delivery_order(
        cod_client, seller, buyer, listing_body, order_body, idempotency_key, COD_BUY_KG
    )
    confirm(cod_client, buyer, order_id)
    confirm(cod_client, seller, order_id)
    shipped(cod_client, seller, order_id, idempotency_key)

    response = attempt(
        cod_client, buyer, f"/v1/orders/{order_id}/confirm-delivery", idempotency_key
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "DELIVERED"
    assert response.json()["delivered_weight_kg"] is None


def test_more_than_was_agreed_is_recorded_and_confirmed_up_to_five_percent(
    cod_client: TestClient,
    cod_signed_in: Callable[..., dict[str, str]],
    cod_signed_in_as_buyer: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    order_body: Callable[..., dict[str, object]],
    idempotency_key: Callable[[], str],
) -> None:
    """Surplus within the cushion is receivable; the shortfall is the only failure.

    A thousand kilos ordered records and confirms one thousand and fifty — the full
    five-percent cushion — because "more than was agreed" is not the failure in transit "less"
    is, up to the bound. The tolerance check is about the side that travels to the buyer.
    """
    seller = cod_signed_in()
    buyer = cod_signed_in_as_buyer()
    order_id, _ = delivery_order(
        cod_client, seller, buyer, listing_body, order_body, idempotency_key, 1000
    )
    confirm(cod_client, buyer, order_id)
    confirm(cod_client, seller, order_id)
    shipped(cod_client, seller, order_id, idempotency_key)
    recorded = record_weight(cod_client, seller, order_id, idempotency_key, 1050)
    assert recorded.status_code == 200, recorded.text

    response = attempt(
        cod_client, buyer, f"/v1/orders/{order_id}/confirm-delivery", idempotency_key
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "DELIVERED"
    assert response.json()["delivered_weight_kg"] == 1050
