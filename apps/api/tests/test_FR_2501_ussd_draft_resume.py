"""FR-2501, the checkpoint: what a dropped USSD call leaves behind, and how a re-dial takes it back.

The network drops a session the moment a handset goes quiet, but a trader who was mid-listing
is not restarting: the half-finished values are worth keeping. The store keeps them under the
phone number as a draft with a fifteen-minute life, long enough to matter and short enough to
go away, and a re-dial offers that draft back. These tests hold the whole mechanism against the
fake store — the write side (the engine checkpointing every step of a flow and deleting on its
last screen) and the read side (the offer, the resume into the exact screen, the discard) — so
no database is invented to prove a Redis TTL was asked for.

The first seven tests below drive the store directly, as the seam under test is
``SessionStore``'s own key and life. The rest walk the real tree through the real engine on a
``FlowGateway`` whose operations answer with the same context keys the production gateway
writes, because a checkpoint is only worth what a resume can finish with.

Marked unit for that reason: the same code, the same inputs, no Postgres and no Redis.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from packages.shared.i18n.languages import Language
from packages.shared.messaging.dispatcher import format_etb
from packages.ussd.engine import handle
from packages.ussd.session import (
    DRAFT_TTL_SECONDS,
    SESSION_TTL_SECONDS,
    SessionState,
    SessionStore,
)
from packages.ussd.trees import build_nodes
from packages.ussd.trees.listings import NEW_LISTING_DRAFT_FIELDS
from packages.ussd.trees.model import Action

pytestmark = pytest.mark.unit

PHONE = "+251911000001"


class FakeStore:
    def __init__(self, seed: dict[str, str] | None = None) -> None:
        self.data: dict[str, str] = dict(seed or {})
        self.expires: dict[str, int] = {}

    async def get(self, name: str) -> Any:
        return self.data.get(name)

    async def set(self, name: str, value: str, *, ex: int | None = None) -> Any:
        self.data[name] = value
        if ex is not None:
            self.expires[name] = ex
        return None

    async def delete(self, *names: str) -> Any:
        for name in names:
            self.data.pop(name, None)
        return 0


class FakeGateway:
    async def resolve_user(self, phone_e164: str) -> str | None:
        return None

    async def user_language(self, user_id: str) -> Language:
        return Language.EN

    async def prefetch(self, name: str, state: SessionState) -> dict[str, Any]:
        return {}

    async def apply(
        self, action: Action, text: str, state: SessionState, session_id: str
    ) -> dict[str, Any]:
        if action.operation == "set_language":
            return {"lang": {"1": Language.AM, "2": Language.EN, "3": Language.OM}[text.strip()]}
        return {}


async def _reply(raw: FakeStore, text: str) -> str:
    return await handle(
        store=SessionStore(raw),
        gateway=FakeGateway(),
        nodes=build_nodes(),
        session_id="ses-draft",
        phone_e164=PHONE,
        text=text,
    )


def _seed_draft(step: str, body: dict[str, Any]) -> FakeStore:
    return FakeStore(
        seed={
            "ussd:lang:" + PHONE: "en",
            "ussd:draft:" + PHONE: json.dumps({"_draft_step": step, "_draft_body": body}),
        }
    )


async def test_a_draft_is_kept_under_the_phone_number_with_its_own_life() -> None:
    """A draft outlives the session it was written in, but only for a quarter of an hour."""
    raw = FakeStore()
    store = SessionStore(raw)
    await store.save_draft(PHONE, {"crop_id": 1, "quantity_kg": "500"})
    assert raw.expires["ussd:draft:" + PHONE] == DRAFT_TTL_SECONDS
    assert (await store.load_draft(PHONE)) == {"crop_id": 1, "quantity_kg": "500"}


async def test_a_session_is_kept_under_the_session_id_with_the_gateway_life() -> None:
    raw = FakeStore()
    store = SessionStore(raw)
    state = SessionState(user_id=None, lang=Language.EN, menu_stack=["main_menu"], context={})
    await store.save("ses-x", state)
    assert raw.expires["ussd:ses-x"] == SESSION_TTL_SECONDS
    restored = await store.load("ses-x")
    assert restored is not None
    assert restored.menu_stack == ["main_menu"]
    assert restored.lang is Language.EN


async def test_a_draft_for_a_known_step_is_offered_back() -> None:
    raw = _seed_draft("offer_confirm", {})
    reply = await _reply(raw, "")
    assert reply.startswith("CON ")
    assert "1. Continue where you left off" in reply


async def test_a_draft_for_an_unknown_step_is_not_offered() -> None:
    """A checkpoint whose step this build does not know is useless and is not offered."""
    raw = _seed_draft("flow_not_in_this_build", {})
    reply = await _reply(raw, "")
    assert reply.startswith("CON ")
    assert "Continue where you left off" not in reply
    assert "1. My Listings" in reply


async def test_discarding_the_draft_writes_nothing_back_to_redis() -> None:
    raw = _seed_draft("offer_confirm", {"crop_id": 1})
    await _reply(raw, "")
    await _reply(raw, "2")
    assert "ussd:draft:" + PHONE not in raw.data


async def test_a_resumed_draft_carries_its_values_forward() -> None:
    """The values saved before the drop are on the screen the resume lands on."""
    raw = _seed_draft("offer_confirm", {"quantity_kg": "1,000", "price": "3000"})
    await _reply(raw, "")
    reply = await _reply(raw, "1")
    assert "Offer 1,000kg @3000 ETB/kg?" in reply


async def test_resuming_does_not_delete_the_draft() -> None:
    """A resume takes values forward but keeps the checkpoint until the flow completes."""
    raw = _seed_draft("offer_confirm", {"quantity_kg": "1,000", "price": "3000"})
    await _reply(raw, "")
    await _reply(raw, "1")
    assert "ussd:draft:" + PHONE in raw.data


# -- walking a flow until the network takes it -----------------------------------
#
# From here on the tests drive the real tree, because a checkpoint is only worth what a
# resume can finish with. ``FlowGateway`` is the production gateway's shape in miniature: the
# same operation names, the same context keys, and text resolved the same way where a wrong
# resolution would hide a broken resume — the crop row is an index into the page on screen
# and the price is parsed as money, so an arrival action fired on a resume keypress would
# change the figure instead of passing quietly.


class FlowGateway:
    """The production gateway's operations, answered from constants instead of services."""

    def __init__(self) -> None:
        self.applied: list[tuple[str, str]] = []

    async def resolve_user(self, phone_e164: str) -> str | None:
        return "user-1"

    async def user_language(self, user_id: str) -> Language:
        return Language.EN

    async def prefetch(self, name: str, state: SessionState) -> dict[str, Any]:
        if name == "listing_counts":
            return {"active_count": "0", "paused_count": "0"}
        if name == "new_listing_crops":
            offset = max(0, int(state.context.get("offset", 0)))
            catalogue = [41, 42, 43]
            page = catalogue[offset : offset + 2]
            rows = [f"{index}. Crop {crop_id}" for index, crop_id in enumerate(page, start=1)]
            has_more = offset + len(page) < len(catalogue)
            if has_more:
                rows.append(f"{len(rows) + 1}. More")
            return {
                "rows": "\n".join(rows),
                "crop_ids": page,
                "has_more_crops": has_more,
                "crop_more_key": str(len(page) + 1),
            }
        if name == "browse_regions":
            return {"region_codes": ["OR"], "rows": "1. Oromia", "last_used": "-"}
        if name == "browse_crops":
            return {"crop_ids": [41], "rows": "1. Crop 41"}
        if name == "browse_results":
            return {"listing_ids": ["listing-1"], "rows": "1. ABC123 Crop 41 1,000kg @3000"}
        if name == "browse_result_detail":
            return {
                "crop": "Crop 41",
                "quantity_kg": "1,000",
                "price": "3000",
                "place": "Addis",
                "distance": "",
                "next": "",
            }
        return {}

    async def apply(
        self, action: Action, text: str, state: SessionState, session_id: str
    ) -> dict[str, Any]:
        op = action.operation
        self.applied.append((op, text))
        value = text.strip().replace(",", "")
        if op == "set_language":
            return {"lang": {"1": Language.AM, "2": Language.EN, "3": Language.OM}[value]}
        if op == "require_listing_profile":
            return {"region_code": "OR", "woreda_id": 1}
        if op == "select_crop":
            rows = state.context.get("crop_ids") or []
            return {"crop_id": rows[int(value) - 1], "offset": 0}
        if op == "set_listing_quantity":
            return {"quantity_kg": int(value)}
        if op == "set_listing_price":
            return {"unit_price_santim": int(value) * 100, "price_etb": int(value)}
        if op == "set_listing_harvest":
            offset_days = {"1": 0, "2": 7, "3": 14, "4": 30}[value]
            day = datetime.now(UTC).date() + timedelta(days=offset_days)
            return {"harvest_date": day.isoformat()}
        if op == "set_listing_delivery":
            options = {"1": "PICKUP", "2": "SELLER_DELIVERY", "3": "TRANSPORT_PARTNER"}
            return {"delivery_options": [options[value]], "delivery_fee_santim": 0}
        if op == "set_listing_delivery_fee":
            return {"delivery_fee_santim": int(value) * 100}
        if op == "publish_listing":
            return {"code": "K7T2QX"}
        if op == "select_region":
            return {"region_code": state.context["region_codes"][int(value) - 1]}
        if op == "select_listing":
            return {
                "selected_listing_id": state.context["listing_ids"][int(value) - 1],
                "selected_listing_code": "ABC123",
            }
        if op == "set_quantity":
            return {"quantity_kg": f"{int(value):,}"}
        if op == "set_price":
            santim = int(value) * 100
            return {"price_santim": santim, "price": format_etb(santim)}
        if op == "offer_place":
            return {"code": str(state.context.get("selected_listing_code") or "")}
        return {}


async def _key(raw: FakeStore, gateway: FlowGateway, text: str, session_id: str) -> str:
    """One press on one session. A new session id is what a re-dial after a drop looks like."""
    return await handle(
        store=SessionStore(raw),
        gateway=gateway,
        nodes=build_nodes(),
        session_id=session_id,
        phone_e164=PHONE,
        text=text,
    )


async def _walk(
    raw: FakeStore, gateway: FlowGateway, keys: list[str], session_id: str
) -> list[str]:
    """Dial, then send each key, returning every screen in order (the dial's first)."""
    replies = [await _key(raw, gateway, "", session_id)]
    for key in keys:
        replies.append(await _key(raw, gateway, key, session_id))
    return replies


def _draft(raw: FakeStore) -> dict[str, Any]:
    """The checkpoint under the phone number, or a failure naming its absence."""
    blob = raw.data.get("ussd:draft:" + PHONE)
    assert blob is not None, "no checkpoint under the phone number"
    draft: dict[str, Any] = json.loads(blob)
    return draft


async def _state(raw: FakeStore, session_id: str) -> SessionState:
    state = await SessionStore(raw).load(session_id)
    assert state is not None
    return state


#: A wizard walked to the price screen and abandoned there: crop row one, five hundred kilos.
#: No press ends the session — a network timeout sends no last call, which is the whole point.
DROPPED_LISTING = ["1", "1", "1", "500"]

#: The browse path to the offer confirm screen: region, crop, result, make offer, 500kg @3000.
DROPPED_OFFER = ["2", "1", "1", "1", "1", "500", "3000"]


async def test_a_wizard_the_network_dropped_is_checkpointed_under_the_number() -> None:
    """The screen they stopped on and everything gathered so far, for FR-2501's fifteen minutes.

    Nothing here ends the session: a timeout sends no last call, so the only proof a dropped
    wizard leaves anything behind is the store itself. The step is the price screen, the body
    holds every field the flow declared that has a value and nothing typed after the drop —
    no price yet, because the price screen is what they never got to answer.
    """
    raw = FakeStore()
    await _walk(raw, FlowGateway(), DROPPED_LISTING, "ses-drop")

    draft = _draft(raw)
    assert draft["_draft_step"] == "ussd_new_listing_price"
    assert draft["_draft_body"] == {
        "crop_id": 41,
        "quantity_kg": 500,
        "region_code": "OR",
        "woreda_id": 1,
        "offset": 0,
    }
    assert raw.expires["ussd:draft:" + PHONE] == DRAFT_TTL_SECONDS
    # The life is SPEC FR-2501's own figure, held as a constant and not as a setting: a
    # quarter of an hour, long enough to survive a dropped call, short enough that a stale
    # half-wizard cannot be walked to the end a week later.
    assert DRAFT_TTL_SECONDS == 15 * 60


async def test_a_re_dial_offers_the_wizard_and_the_resume_restores_every_field() -> None:
    """A second session finds the checkpoint, offers it, and on acceptance carries the values.

    The proof of "restores the exact state" is the comparison: every field the flow declares
    holds the same value in the new session as it did in the one the network took — crop,
    quantity, region, woreda, paging position — and the screen they land on is the price
    screen they were answering when the call died.
    """
    raw = FakeStore()
    await _walk(raw, FlowGateway(), DROPPED_LISTING, "ses-drop")
    dropped = await _state(raw, "ses-drop")

    redial = FlowGateway()
    offer = await _key(raw, redial, "", "ses-redial")
    assert "1. Continue where you left off" in offer
    assert "2. Start over" in offer
    landed = await _key(raw, redial, "1", "ses-redial")
    assert landed == "CON Price per kg in ETB?"

    resumed = await _state(raw, "ses-redial")
    assert resumed.menu_stack[-1] == "ussd_new_listing_price"
    for field in NEW_LISTING_DRAFT_FIELDS:
        assert resumed.context.get(field) == dropped.context.get(field), field


async def test_the_resume_keypress_is_never_read_as_a_crop_row() -> None:
    """The landing screen's arrival action answers the screen *before* it — of which there is none.

    ``select_crop`` commits the row pressed on the crop menu. On a resume landing the press is
    "1", a choice about the checkpoint, not a row: the crop page is not even on screen. The
    trader here picked the second row, so a landing that ran the action anyway would leave the
    wizard resumed on a different crop than the one they chose.
    """
    raw = FakeStore()
    await _walk(raw, FlowGateway(), ["1", "1", "2"], "ses-drop")
    draft = _draft(raw)
    assert draft["_draft_step"] == "ussd_new_listing_quantity"
    assert draft["_draft_body"]["crop_id"] == 42

    redial = FlowGateway()
    await _key(raw, redial, "", "ses-redial")
    reply = await _key(raw, redial, "1", "ses-redial")
    assert reply == "CON Quantity in kg?"

    resumed = await _state(raw, "ses-redial")
    assert resumed.context["crop_id"] == 42
    assert not any(op == "select_crop" for op, _ in redial.applied)


async def test_start_over_clears_a_listing_draft_and_presents_the_main_menu() -> None:
    """The second row of the prompt does both halves at once: the checkpoint is gone and the
    trader is back at the top, with no ``_draft`` keys left in the session to be offered again."""
    raw = FakeStore()
    await _walk(raw, FlowGateway(), DROPPED_LISTING, "ses-drop")

    redial = FlowGateway()
    await _key(raw, redial, "", "ses-redial")
    reply = await _key(raw, redial, "2", "ses-redial")
    assert reply.startswith("CON ")
    assert "1. My Listings" in reply
    assert "ussd:draft:" + PHONE not in raw.data
    resumed = await _state(raw, "ses-redial")
    assert "_draft_step" not in resumed.context
    assert "_draft_body" not in resumed.context


async def test_publishing_a_resumed_wizard_writes_one_listing_and_owes_no_draft() -> None:
    """The flow's last screen deletes the checkpoint instead of refreshing it.

    Three things follow from that one line, and all three are asserted here: the publish
    screen's own arrival action fires exactly once, the key under the phone number is gone the
    moment the listing exists, and the next dial finds nothing to offer — a finished wizard is
    never resumed, so no later session can walk one to a second publish.
    """
    raw = FakeStore()
    await _walk(raw, FlowGateway(), DROPPED_LISTING, "ses-drop")

    redial = FlowGateway()
    await _key(raw, redial, "", "ses-redial")
    await _key(raw, redial, "1", "ses-redial")
    for key in ["2500", "1", "1"]:
        reply = await _key(raw, redial, key, "ses-redial")

    assert reply.startswith("END Listing K7T2QX published")
    assert "ussd:draft:" + PHONE not in raw.data
    assert [op for op, _ in redial.applied].count("publish_listing") == 1

    third = FlowGateway()
    again = await _key(raw, third, "", "ses-third")
    assert "Continue where you left off" not in again
    assert "1. My Listings" in again


async def test_a_dropped_offer_is_offered_back_on_its_confirm_screen_with_its_numbers() -> None:
    """FR-2501 says "listing/offer", and the offer path checkpoints the same way.

    The confirm screen is where the drop lands, so its arrival action is the dangerous one:
    ``set_price`` parses whatever text it is handed, and the resume's own "1" would have
    rewritten three thousand birr as one hundred santim. The restored figure on screen and in
    the session is the proof that the draft — not the keypress — is the answer here.
    """
    raw = FakeStore()
    await _walk(raw, FlowGateway(), DROPPED_OFFER, "ses-drop")

    draft = _draft(raw)
    assert draft["_draft_step"] == "offer_confirm"
    assert draft["_draft_body"] == {
        "selected_listing_id": "listing-1",
        "selected_listing_code": "ABC123",
        "quantity_kg": "500",
        "price_santim": 300000,
        "price": "3000",
    }

    redial = FlowGateway()
    offer = await _key(raw, redial, "", "ses-redial")
    assert "1. Continue where you left off" in offer
    reply = await _key(raw, redial, "1", "ses-redial")
    assert "Offer 500kg @3000 ETB/kg?" in reply

    resumed = await _state(raw, "ses-redial")
    assert resumed.context["quantity_kg"] == "500"
    assert resumed.context["price_santim"] == 300000
    assert not any(op == "set_price" for op, _ in redial.applied)


async def test_placing_a_resumed_offer_writes_it_once_and_clears_the_checkpoint() -> None:
    """Offer and listing end the same way: the last screen owes nothing to resume."""
    raw = FakeStore()
    await _walk(raw, FlowGateway(), DROPPED_OFFER, "ses-drop")

    redial = FlowGateway()
    await _key(raw, redial, "", "ses-redial")
    await _key(raw, redial, "1", "ses-redial")
    reply = await _key(raw, redial, "1", "ses-redial")

    assert reply.startswith("END ")
    assert "ABC123" in reply
    assert "ussd:draft:" + PHONE not in raw.data
    assert [op for op, _ in redial.applied].count("offer_place") == 1


async def test_one_number_holds_one_draft_and_the_newest_flow_is_what_is_offered() -> None:
    """One phone, one checkpoint: the flow started last is the one a re-dial resumes.

    The single key is deliberate rather than a limitation — a trader gets one unfinished thing
    at a time, and walking back out of the listing to start an offer overwrites the listing's
    checkpoint instead of forking it. The proof is both halves: exactly one draft key exists
    at the end, and it holds the offer, not the listing that was there first.
    """
    raw = FakeStore()
    await _walk(raw, FlowGateway(), ["1", "1"], "ses-listing")
    assert _draft(raw)["_draft_step"] == "ussd_new_listing_crop"

    second = FlowGateway()
    await _key(raw, second, "", "ses-redial")
    await _key(raw, second, "1", "ses-redial")  # back into the listing they left...
    await _key(raw, second, "00", "ses-redial")  # ...and out to the main menu
    for key in ["2", "1", "1", "1", "1", "500", "3000"]:
        await _key(raw, second, key, "ses-redial")

    drafts = [name for name in raw.data if name.startswith("ussd:draft:")]
    assert drafts == ["ussd:draft:" + PHONE]
    assert _draft(raw)["_draft_step"] == "offer_confirm"

    third = FlowGateway()
    await _key(raw, third, "", "ses-third")
    reply = await _key(raw, third, "1", "ses-third")
    assert "Offer 500kg @3000 ETB/kg?" in reply
