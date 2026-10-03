"""Where a verification code lives between being sent and being checked.

A code is never stored in readable form. What we keep is a digest of the code together
with the salt that produced it, which is enough to recognise a correct guess and useless
to anyone who copies the store: a six-digit code is small enough to search by brute force
unless every attempt is salted and counted.

The counters around it are what make the code safe to send over SMS: the number of wrong
guesses it tolerates, the gap before another code may be sent, and the mark left behind once
those guesses are spent. The ceilings on how many codes a number or an address may receive in
a day belong to the abuse controls rather than here, so there is one counter per rule.
"""

from __future__ import annotations

from dataclasses import dataclass

from redis.asyncio import Redis
from redis.typing import EncodableT


def _as_text(value: EncodableT) -> str:
    """Read a stored value as text whichever way the client is configured to return it."""
    return value.decode() if isinstance(value, bytes) else str(value)


@dataclass(frozen=True, slots=True)
class StoredCode:
    """A pending verification code, in the only form it is ever kept in."""

    digest: str
    salt: str


@dataclass(frozen=True, slots=True)
class FailedGuess:
    """The outcome of a wrong guess: how many guesses are left, if any."""

    attempts_used: int
    attempts_remaining: int


class VerificationCodeRepository:
    """Stores pending codes and the counters that police them."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    # Key layout. Phone numbers and addresses appear in key names, never in key values,
    # and every key is prefixed so a future scan or eviction policy can target one kind
    # of data without touching the other.
    @staticmethod
    def _code_key(phone_e164: str) -> str:
        return f"verification-code:{phone_e164}"

    @staticmethod
    def _attempts_key(phone_e164: str) -> str:
        return f"verification-attempts:{phone_e164}"

    @staticmethod
    def _cooldown_key(phone_e164: str) -> str:
        return f"verification-cooldown:{phone_e164}"

    @staticmethod
    def _lock_key(phone_e164: str) -> str:
        return f"verification-locked:{phone_e164}"

    async def store(self, phone_e164: str, digest: str, salt: str, ttl_seconds: int) -> None:
        """Remember a freshly sent code and reset the guess counter for it.

        The counter resets on every new code: the limit is five wrong guesses per code,
        not five per day, so replacing the code gives the sender a clean slate. A lock from
        the previous code is lifted at the same moment, because the new code is what the
        owner is now expected to use.
        """
        key = self._code_key(phone_e164)
        await self._redis.hset(key, mapping={"digest": digest, "salt": salt})
        await self._redis.expire(key, ttl_seconds)
        await self._redis.delete(self._attempts_key(phone_e164))
        await self._redis.delete(self._lock_key(phone_e164))

    async def load(self, phone_e164: str) -> StoredCode | None:
        """The pending code, or nothing if it expired or was destroyed."""
        data = await self._redis.hgetall(self._code_key(phone_e164))
        if not data or "digest" not in data or "salt" not in data:
            return None
        return StoredCode(
            digest=_as_text(data["digest"]),
            salt=_as_text(data["salt"]),
        )

    async def spend(self, phone_e164: str) -> None:
        """Erase the code and its counter so it can never be tried again.

        Used when a code is redeemed and when a message fails to go out, so a code the
        owner never received does not quietly consume one of their guesses.
        """
        await self._redis.delete(
            self._code_key(phone_e164),
            self._attempts_key(phone_e164),
            self._cooldown_key(phone_e164),
        )

    async def lock(self, phone_e164: str, ttl_seconds: int) -> None:
        """Retire the code and mark this number as locked until a new code is sent.

        The mark outlives the code on purpose. Once the guesses are spent there is nothing
        left to guess, but a number that stays locked answers every later attempt the same
        way instead of sliding back to "no code pending" — which is what tells a client the
        difference between waiting and starting over.
        """
        await self.spend(phone_e164)
        await self._redis.set(self._lock_key(phone_e164), "1", ex=ttl_seconds)

    async def is_locked(self, phone_e164: str) -> bool:
        """Whether this number has spent its guesses on the current code."""
        return bool(await self._redis.exists(self._lock_key(phone_e164)))

    async def register_failed_guess(self, phone_e164: str, max_attempts: int) -> FailedGuess:
        """Count a wrong guess and report how many guesses are left.

        Redis increments atomically, so two simultaneous wrong guesses cannot both be
        admitted as the last one.
        """
        attempts_used = int(await self._redis.incr(self._attempts_key(phone_e164)))
        return FailedGuess(
            attempts_used=attempts_used,
            attempts_remaining=max(0, max_attempts - attempts_used),
        )

    async def attempts_used(self, phone_e164: str) -> int:
        """Wrong guesses already made against the current code."""
        raw = await self._redis.get(self._attempts_key(phone_e164))
        return int(raw) if raw else 0

    async def cooldown_remaining(self, phone_e164: str) -> int:
        """Seconds before this number may be sent another code."""
        ttl = await self._redis.ttl(self._cooldown_key(phone_e164))
        return max(0, int(ttl)) if ttl and ttl > 0 else 0

    async def start_cooldown(self, phone_e164: str, seconds: int) -> None:
        """Block another code to this number for a while.

        A trader who taps "send" twice, or a client retrying on a slow network, should not
        burn two codes and confuse the person reading their messages.
        """
        await self._redis.set(self._cooldown_key(phone_e164), "1", ex=seconds)
