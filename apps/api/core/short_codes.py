"""The two codes a person is expected to read out loud.

An order carries two of them and they are not interchangeable. The `GL-` short code identifies
the order to the platform, to the seller and to support, and is derived from Crockford's Base32;
the pickup code is six digits a buyer shows a seller at the moment of collection, and is derived
from nothing but the clock.

Crockford's alphabet omits I, L, O and U. That omission is the entire reason this alphabet is
used instead of the full Base32 one: a code written down from a text message and read aloud over
a telephone line must not be mistakable for a different code, and letters that sound alike or
read alike are the ones to leave out. `0` rather than `O` is part of the same bargain.

The prefix is stored, not added at display time. FR-608 is explicit that the stored value
includes `GL-` and that every surface shows the stored value verbatim, and the reason is worth
keeping in mind: an SMS, a USSD menu and an API response that each stripped or rebuilt the prefix
would each be a place where a code could disagree with the others, and a code that disagrees
with itself is a code nobody can read.

Codes are drawn from `secrets`, never from a seeded generator, because a code is the only handle
on somebody's order and a predictable one would let one party guess another's.
"""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable
from typing import Protocol

from packages.shared.errors import ApiError, ErrorCode

#: Crockford's alphabet: ten digits and twenty-two letters, with I, L, O and U left out.
CROCKFORD_BASE32 = "0123456789ABCDEFGHJKMNPQRSTWXYZ"

#: What an order's code is prefixed with, stored as part of the code rather than shown around it.
ORDER_CODE_PREFIX = "GL-"

#: Six characters after the prefix, thirty bits of alphabet, around a billion values. Long enough
#: that a collision is a curiosity; short enough that a human reads it back without complaint.
ORDER_CODE_LENGTH = 6

#: How many draws before a collision is treated as a fault rather than as bad luck. Eight draws
#: puts the chance of exhausting them below one in a billion billion, so reaching it means the
#: uniqueness check is wrong rather than unlucky.
ORDER_CODE_ATTEMPTS = 8

#: Digits only, and six of them. A pickup code is read at a roadside or in a market by somebody
#: who is not typing into a form, so letters are out - one and seven, zero and eight are the
#: mistakes that matter - and no prefix, because the seller and the buyer are looking at each
#: other and already know what the number is for.
PICKUP_CODE_LENGTH = 6


class OrderCodeSource(Protocol):
    """Whatever draws order codes, so that the rule can be tested against a known sequence."""

    async def draw(self) -> str: ...


class CrockfordOrderCode:
    """Draws order codes that no other order holds."""

    def __init__(self, taken: Callable[[str], Awaitable[bool]]) -> None:
        self._taken = taken

    async def draw(self) -> str:
        """A code with the prefix, six of the alphabet's characters, and nothing else.

        The table is asked whether the code is taken *before* the row is written, so a collision
        is an ordinary retry rather than a failed transaction the caller sees. The unique
        constraint underneath is still what makes the property true; asking first only makes
        hitting it rare enough to read as an event rather than a bug.
        """
        for _ in range(ORDER_CODE_ATTEMPTS):
            candidate = ORDER_CODE_PREFIX + "".join(
                secrets.choice(CROCKFORD_BASE32) for _ in range(ORDER_CODE_LENGTH)
            )
            if not await self._taken(candidate):
                return candidate
        raise ApiError(  # pragma: no cover - needs a billion codes already taken
            ErrorCode.INTERNAL_ERROR, details={"reason": "order_short_code_exhausted"}
        )


class ScriptedOrderCode:
    """A code source that hands out a fixed sequence, so a test can watch a collision be retried.

    Used in place of `CrockfordOrderCode` when the point of the test is what happens on the
    second draw. A generator that could be steered from outside is the only way to reach that
    branch without filling a billion rows first.
    """

    def __init__(self, codes: list[str]) -> None:
        self._codes = list(codes)
        self.draws = 0

    async def draw(self) -> str:
        if not self._codes:  # pragma: no cover - a test would have to ask for too many
            raise ApiError(ErrorCode.INTERNAL_ERROR, details={"reason": "no_code_left_to_give"})
        self.draws += 1
        return self._codes.pop(0)


def new_pickup_code() -> str:
    """Six digits, drawn fresh.

    Not checked against anything. A pickup code is only ever compared with a code a seller is
    holding for the same order, and its job is to be unguessable by somebody standing in a
    market rather than to be globally unique.
    """
    return "".join(str(secrets.randbelow(10)) for _ in range(PICKUP_CODE_LENGTH))
