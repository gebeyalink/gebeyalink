"""FR-2501, the checkpoint: what a dropped USSD call leaves behind, and how a re-dial takes it back.

The network drops a session the moment a handset goes quiet, but a trader who was mid-listing
is not restarting: the half-finished values are worth keeping. The store keeps them under the
phone number as a draft with a fifteen-minute life, long enough to matter and short enough to
go away, and a re-dial offers that draft back. These tests hold the mechanism — what lands in
the store, which step a resume walks into, that a discarded draft is gone — against the fake
store, so no database is invented to prove a Redis TTL was asked for.

Marked unit for that reason: the seam under test is ``SessionStore`` and the engine's resume
effects, both of which are pure code.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from packages.shared.i18n.languages import Language
from packages.ussd.engine import handle
from packages.ussd.session import (
    DRAFT_TTL_SECONDS,
    SESSION_TTL_SECONDS,
    SessionState,
    SessionStore,
)
from packages.ussd.trees import build_nodes
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
    assert "1. Resume previous listing?" in reply


async def test_a_draft_for_an_unknown_step_is_not_offered() -> None:
    """A checkpoint whose step this build does not know is useless and is not offered."""
    raw = _seed_draft("flow_not_in_this_build", {})
    reply = await _reply(raw, "")
    assert reply.startswith("CON ")
    assert "Resume previous listing?" not in reply
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
