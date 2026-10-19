"""FR-1101, the session engine: one keypress in, one plain-text screen out.

The engine is where a USSD session's shape lives - where a fresh dial starts, how a choice
is refused, how a draft resumes, what an ended screen does with a replayed press - and none
of it touches a database. Everything it needs arrives as a session, a store and a gateway,
so these tests drive the real engine against a fake store (Redis replaced by a dict) and a
fake gateway (the platform replaced by nothing at all). What the fake cannot provide, the
engine must merely ask for, and asserting the asks is half of what these tests do.

Marked unit for that reason: the same code, the same inputs, no Postgres and no Redis.
"""

from __future__ import annotations

from typing import Any

import pytest

from packages.shared.i18n.languages import Language
from packages.ussd.engine import handle
from packages.ussd.session import SessionState, SessionStore
from packages.ussd.trees import build_nodes
from packages.ussd.trees.model import Action, Node

pytestmark = pytest.mark.unit

PHONE = "+251911000001"


class FakeStore:
    """The three calls the store needs, over an in-memory dict with expiries ignored."""

    def __init__(self, seed: dict[str, str] | None = None) -> None:
        self.data: dict[str, str] = dict(seed or {})

    async def get(self, name: str) -> Any:
        return self.data.get(name)

    async def set(self, name: str, value: str, *, ex: int | None = None) -> Any:
        self.data[name] = value
        return None

    async def delete(self, *names: str) -> Any:
        for name in names:
            self.data.pop(name, None)
        return 0


class FakeGateway:
    """A gateway that is all surface: every ask answered with empty, honest defaults."""

    def __init__(self, *, users: dict[str, str] | None = None) -> None:
        self.users = dict(users or {})
        self.applied: list[tuple[Action, str]] = []
        self.prefetches: list[str] = []

    async def resolve_user(self, phone_e164: str) -> str | None:
        return self.users.get(phone_e164)

    async def user_language(self, user_id: str) -> Language:
        return Language.EN

    async def prefetch(self, name: str, state: SessionState) -> dict[str, Any]:
        self.prefetches.append(name)
        if name == "listing_counts":
            return {"active_count": "0", "paused_count": "0"}
        if name == "browse_regions":
            return {"region_codes": ["OR"], "rows": "1. Oromia", "last_used": "-"}
        if name == "browse_crops":
            return {"crop_ids": [1], "rows": "1. Teff"}
        return {}

    async def apply(
        self, action: Action, text: str, state: SessionState, session_id: str
    ) -> dict[str, Any]:
        self.applied.append((action, text))
        if action.operation == "set_language":
            return {"lang": {"1": Language.AM, "2": Language.EN, "3": Language.OM}[text.strip()]}
        if action.operation == "select_region":
            return {"region_code": "OR"}
        if action.operation == "select_crop":
            return {"crop_id": 1, "offset": 0}
        if action.operation == "set_quantity":
            return {"quantity_kg": f"{int(text.strip()):,}"}
        if action.operation == "set_price":
            return {"price_santim": 3_000_000, "price": "3000"}
        return {}


@pytest.fixture()
def engine() -> dict[str, Node]:
    return build_nodes()


async def _dial(
    *,
    nodes: dict[str, Node],
    store: SessionStore,
    gateway: FakeGateway,
    session_id: str = "ses-test-1",
    phone: str = PHONE,
    text: str = "",
) -> str:
    return await handle(
        store=store,
        gateway=gateway,
        nodes=nodes,
        session_id=session_id,
        phone_e164=phone,
        text=text,
    )


def _screen(reply: str) -> str:
    assert reply.startswith(("CON ", "END ")), reply
    return reply[4:]


async def _session(store: SessionStore, session_id: str = "ses-test-1") -> SessionState:
    state = await store.load(session_id)
    assert state is not None
    return state


async def test_a_fresh_dial_asks_for_a_language(engine: dict[str, Node]) -> None:
    store = SessionStore(FakeStore())
    reply = await _dial(nodes=engine, store=store, gateway=FakeGateway(), text="")
    assert reply.startswith("CON ")
    assert "ገበያሊንክ እንኳን ደህና መጡ" in reply
    assert "3. Afaan Oromoo" in reply


async def test_choosing_a_language_opens_the_main_menu_and_remembers_it(
    engine: dict[str, Node],
) -> None:
    raw = FakeStore()
    store = SessionStore(raw)
    gateway = FakeGateway()
    await _dial(nodes=engine, store=store, gateway=gateway)
    reply = await _dial(nodes=engine, store=store, gateway=gateway, text="2")
    assert reply.startswith("CON ")
    body = _screen(reply)
    assert "1. My Listings" in body
    assert "5. Help" in body
    assert await store.load_language(PHONE) is Language.EN


async def test_a_registered_handset_starts_on_the_main_menu(
    engine: dict[str, Node],
) -> None:
    store = SessionStore(FakeStore())
    gateway = FakeGateway(users={PHONE: "user-1"})
    reply = await _dial(nodes=engine, store=store, gateway=gateway)
    assert reply.startswith("CON ")
    assert "1. My Listings" in reply
    assert "Welcome to GebeyaLink" not in reply


async def test_the_listings_path_is_gated_for_a_new_handset(
    engine: dict[str, Node],
) -> None:
    """FR-1101's ruling: a number nobody has registered reaches the listings menus nowhere."""

    store = SessionStore(FakeStore())
    gateway = FakeGateway()
    await _dial(nodes=engine, store=store, gateway=gateway)
    await _dial(nodes=engine, store=store, gateway=gateway, text="2")
    reply = await _dial(nodes=engine, store=store, gateway=gateway, text="1")
    assert reply.startswith("END ")
    assert reply == "END This number is not registered. To register, dial *851#."


async def test_the_same_path_is_open_to_a_registered_handset(
    engine: dict[str, Node],
) -> None:
    store = SessionStore(FakeStore())
    gateway = FakeGateway(users={PHONE: "user-1"})
    await _dial(nodes=engine, store=store, gateway=gateway)
    reply = await _dial(nodes=engine, store=store, gateway=gateway, text="1")
    assert reply.startswith("CON ")
    assert "2. Active (0)" in reply


async def test_a_refused_choice_answers_with_the_failed_reason(
    engine: dict[str, Node],
) -> None:
    store = SessionStore(FakeStore())
    gateway = FakeGateway()
    await _dial(nodes=engine, store=store, gateway=gateway)
    await _dial(nodes=engine, store=store, gateway=gateway, text="2")
    reply = await _dial(nodes=engine, store=store, gateway=gateway, text="9")
    # The reason line alone, not the reason with the menu under it: the two together are past the
    # channel's one-screen budget (D-22), so the menu is not re-rendered.
    assert reply == "CON ❌ Invalid choice. Try again."


async def test_the_fourth_refused_choice_hands_the_session_to_help(
    engine: dict[str, Node],
) -> None:
    store = SessionStore(FakeStore())
    gateway = FakeGateway()
    await _dial(nodes=engine, store=store, gateway=gateway)
    await _dial(nodes=engine, store=store, gateway=gateway, text="2")
    for _ in range(4):
        reply = await _dial(nodes=engine, store=store, gateway=gateway, text="9")
    assert reply == "END Call 951 free / SMS HELP. Agents in your kebele can assist."


async def test_a_resumed_draft_leads_into_the_saved_step(
    engine: dict[str, Node],
) -> None:
    """FR-2501: a dial that finds a checkpoint offers it, and the first choice resumes it."""

    raw = FakeStore(
        seed={
            "ussd:lang:" + PHONE: "en",
            "ussd:draft:" + PHONE: '{"_draft_step": "offer_confirm",'
            ' "_draft_body": {"quantity_kg": "1,000", "price": "3000"}}',
        }
    )
    store = SessionStore(raw)
    reply = await _dial(nodes=engine, store=store, gateway=FakeGateway())
    assert reply.startswith("CON ")
    assert "1. Resume previous listing?" in reply
    assert "2. Main Menu" in reply

    reply = await _dial(nodes=engine, store=store, gateway=FakeGateway(), text="1")
    assert reply.startswith("CON ")
    assert "Offer 1,000kg @3000 ETB/kg?" in reply


async def test_a_resumed_draft_that_is_discarded_returns_to_the_main_menu(
    engine: dict[str, Node],
) -> None:
    raw = FakeStore(
        seed={
            "ussd:lang:" + PHONE: "en",
            "ussd:draft:" + PHONE: '{"_draft_step": "offer_confirm", "_draft_body": {}}',
        }
    )
    store = SessionStore(raw)
    # A fresh dial shows the resume prompt and drops its input; the discard is the next press.
    await _dial(nodes=engine, store=store, gateway=FakeGateway())
    reply = await _dial(nodes=engine, store=store, gateway=FakeGateway(), text="2")
    assert reply.startswith("CON ")
    assert "1. My Listings" in reply
    assert await store.load_draft(PHONE) is None
    assert "_draft_step" not in (await _session(store)).context


async def test_a_phone_that_does_not_match_the_session_starts_fresh(
    engine: dict[str, Node],
) -> None:
    """FR-1101's ruling: a session belongs to the number that dialled it, and to no other."""

    store = SessionStore(FakeStore())
    gateway = FakeGateway()
    await _dial(nodes=engine, store=store, gateway=gateway)
    await _dial(nodes=engine, store=store, gateway=gateway, text="2")
    reply = await _dial(nodes=engine, store=store, gateway=gateway, phone="+251911000099", text="3")
    assert reply.startswith("CON ")
    assert "3. Afaan Oromoo" in reply


async def test_zero_returns_to_the_menu_above(engine: dict[str, Node]) -> None:
    store = SessionStore(FakeStore())
    gateway = FakeGateway(users={PHONE: "user-1"})
    await _dial(nodes=engine, store=store, gateway=gateway)
    await _dial(nodes=engine, store=store, gateway=gateway, text="2")
    # On the browse path, ``0`` climbs back to the main menu and re-renders it.
    reply = await _dial(nodes=engine, store=store, gateway=gateway, text="0")
    assert reply.startswith("CON ")
    assert "1. My Listings" in reply


async def test_a_terminal_screen_simply_rerenders_on_a_replayed_press(
    engine: dict[str, Node],
) -> None:
    """An input that lands on an ended session must not fire its action a second time."""

    raw = FakeStore(
        seed={
            "ussd:lang:" + PHONE: "en",
            "ussd:draft:" + PHONE: '{"_draft_step": "ussd_new_listing_published",'
            ' "_draft_body": {"code": "K7T2QX"}}',
        }
    )
    store = SessionStore(raw)
    gateway = FakeGateway()
    await _dial(nodes=engine, store=store, gateway=gateway)
    reply = await _dial(nodes=engine, store=store, gateway=gateway, text="1")
    assert reply.startswith("END ")
    assert "K7T2QX" in reply
    outstanding = len(gateway.applied)
    assert outstanding == 1
    reply = await _dial(nodes=engine, store=store, gateway=gateway, text="1")
    assert reply.startswith("END ")
    assert "K7T2QX" in reply
    assert len(gateway.applied) == outstanding


async def test_an_empty_press_on_a_live_session_ends_it_quietly(
    engine: dict[str, Node],
) -> None:
    store = SessionStore(FakeStore())
    gateway = FakeGateway()
    await _dial(nodes=engine, store=store, gateway=gateway)
    await _dial(nodes=engine, store=store, gateway=gateway, text="2")
    reply = await _dial(nodes=engine, store=store, gateway=gateway, text="")
    assert reply.startswith("END ")
    assert "1. My Listings" in reply


async def test_a_corrupt_session_is_treated_as_no_session_at_all(
    engine: dict[str, Node],
) -> None:
    """A stuck session must not be salvaged; the input that arrived with it is dropped."""

    raw = FakeStore(seed={"ussd:ses-broken": "this is not a session"})
    store = SessionStore(raw)
    reply = await _dial(
        nodes=engine,
        store=store,
        gateway=FakeGateway(),
        session_id="ses-broken",
        text="5",
    )
    assert reply.startswith("CON ")
    assert "1. አማርኛ" in reply
    assert "3. Afaan Oromoo" in reply
