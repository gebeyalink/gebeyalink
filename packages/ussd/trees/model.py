"""What a menu tree is made of: nodes, the actions a choice can trigger, and the node's move.

A tree is data. A node says what it renders, which registry entries those are, what its choice
keys mean, and — when the choice must reach the platform — which operation to run and on what.
It does not call a service and does not know a session id; the engine drives it and the
gateway performs the operations. That split is what lets the whole menu structure be read as a
table and asserted without a database.

Two kinds of action are pinned here because the difference is real rather than stylistic. A
source action fires while standing on the node the trader just answered, on the input they
gave; the language menu uses one, because the digit picked is the language. An arrival action
fires after the move, on the input that brought the trader there; placing an offer uses one,
because the numbers were entered one screen earlier and the move is what commits them.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from packages.ussd.session import SessionState


@dataclass(frozen=True, slots=True)
class Action:
    """Something the platform must do because of a choice.

    ``entity_from`` names the context key the operation acts on — a listing, an offer, an
    order — and ``indexed`` says the choice is a row number rather than a value, so the
    gateway resolves it against the list the menu showed.
    """

    operation: str
    entity_from: str | None = None
    indexed: bool = False


NextFn = Callable[[str, SessionState], "str | None"]


@dataclass(frozen=True, slots=True)
class Node:
    """One screen: what it shows, where a choice leads, and what the move does."""

    id: str
    prompt: tuple[str, ...]
    next: NextFn | None = None
    terminal: bool = False
    requires_account: bool = False
    prefetch: tuple[str, ...] = ()
    action: Action | None = None
    arrival_action: Action | None = None
    resumes_draft: bool = False
    allows_back: bool = True
    error_reason_id: str | None = None
    draft_fields: tuple[str, ...] = ()
    """The context keys a flow has collected, checkpointed on every landing (FR-2501).

    A node that names fields is a step of a resumable flow: the engine writes what the flow
    has gathered so far under the dialling number, so a call the network drops mid-wizard can
    be walked on from this very screen rather than from the top. The same fields on a
    *terminal* node mean the flow has finished — the publish screen, the placed-offer screen —
    so landing there deletes the checkpoint instead of refreshing it: a finished flow owes
    nothing to resume, and a stale one must never be replayed into a second listing.
    """


def choices(mapping: Mapping[str, str]) -> NextFn:
    """A pure move that reads a fixed table of keys, refusing anything else."""

    table = dict(mapping)

    def _next(text: str, session: SessionState) -> str | None:
        return table.get(text.strip())

    return _next


def index_choice(target: str, ids_key: str) -> NextFn:
    """A move that accepts a row number within the list the previous screen showed."""

    def _next(text: str, session: SessionState) -> str | None:
        value = text.strip()
        if not value.isdigit():
            return None
        ids = session.context.get(ids_key)
        if not isinstance(ids, list):
            return None
        position = int(value)
        if 1 <= position <= len(ids):
            return target
        return None

    return _next


def digits(target: str) -> NextFn:
    """A move that accepts any run of digits as a value and carries on."""

    def _next(text: str, session: SessionState) -> str | None:
        value = text.strip()
        return target if value.isdigit() else None

    return _next


def positive_amount(target: str) -> NextFn:
    """A move that accepts a non-zero run of digits, for a quantity or a price."""

    def _next(text: str, session: SessionState) -> str | None:
        value = text.strip().replace(",", "")
        if not value.isdigit():
            return None
        return target if int(value) > 0 else None

    return _next


def context_key(value_key: str) -> Callable[[SessionState], Any]:
    """Read one context value, for a move that needs to know what was chosen."""

    def _read(session: SessionState) -> Any:
        return session.context.get(value_key)

    return _read
