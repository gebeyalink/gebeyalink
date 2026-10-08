"""The single-use tokens that make a sensitive SMS command friction against SIM-swap fraud.

FR-1102 puts an action token between a notification SMS and the two commands that move
money or goods on somebody's behalf: accepting an offer and confirming a delivery. The
token is short enough to be read from a text message and typed back, and single-use, so an
attacker who has swapped a SIM and taken over the number still cannot act on a notification
the real owner read first.

Two things are decided here rather than by a caller.

The stored value is a hash, never the token. The token itself is a short string; a table of
short strings is a table somebody can read, so what is written is the SHA-256 of the
normalised token, and a lookup is by that digest. Normalisation is strip-and-upper because
the token crosses a handset that may add spaces and a person who may change the case.

A purpose is a string with a prefix that names what the token unlocks and the identifier it
unlocks it for — ``offer_answer:<uuid>`` or ``delivery_confirm:<uuid>``. Keeping the target
in the purpose is what lets the handler branch without a second column, and what lets a
token for one offer be useless for another.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Final

#: What an action token is for. The prefix is the whole of what a handler branches on.
OFFER_ANSWER: Final[str] = "offer_answer"
DELIVERY_CONFIRM: Final[str] = "delivery_confirm"


def hash_token(raw: str) -> str:
    """The digest stored for a token, from the token as a trader typed it.

    Strips surrounding whitespace and upper-cases, because the token is read from a message
    and re-keyed on a feature phone where neither is under the sender's control. The hash is
    plain SHA-256: the value being protected is short-lived and single-use, so the point is
    that the raw token is not in the table, not that the digest is slow to guess.
    """
    return hashlib.sha256(raw.strip().upper().encode("utf-8")).hexdigest()


def offer_answer_purpose(offer_id: uuid.UUID) -> str:
    """The purpose of a token that lets its holder answer one offer."""
    return f"{OFFER_ANSWER}:{offer_id}"


def delivery_confirm_purpose(order_id: uuid.UUID) -> str:
    """The purpose of a token that lets its holder confirm one order's delivery."""
    return f"{DELIVERY_CONFIRM}:{order_id}"


def parse_purpose(purpose: str) -> tuple[str, str] | None:
    """Split a purpose into its kind and target, or return ``None`` if it is not one.

    A purpose that is not exactly a known prefix, a colon, and a UUID is refused here rather
    than by the database: the handler treats "not a purpose this platform issues" and "no
    such token" as the same sentence to the trader.
    """
    kind, separator, target = purpose.partition(":")
    if not separator or kind not in (OFFER_ANSWER, DELIVERY_CONFIRM):
        return None
    try:
        parsed = uuid.UUID(target)
    except ValueError:
        return None
    return kind, str(parsed)
