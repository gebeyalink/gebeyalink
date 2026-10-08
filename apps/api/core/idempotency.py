"""Idempotency: one `Idempotency-Key`, one effect.

A buyer on a slow connection taps "buy now" and the request is slow for a reason neither of them
can see. They tap it again. Without this, two buyers' worth of nothing happens: two orders, two
sets of reserved kilos, and one seller whose last hundred kilos have been sold twice.

The rule is that a key identifies an *intent*, and the first request to arrive under a key
decides what that intent was. Everyone who arrives afterwards with the same key gets that same
answer back, byte for byte, without the work being done again. The answer is stored rather than
recomputed, because recomputing it would be doing the work again.

Three properties are worth stating, because each is a decision rather than an implementation
detail.

A claim is taken before the work starts and the result is written after it commits. In between,
a second request carrying the same key finds a claim in flight and is refused with
`CONCURRENT_MODIFICATION` rather than being made to wait for a transaction it cannot see the
end of. That is also the answer to FR-1903's 409: it is reachable here, where two requests are
genuinely in flight at once, and not in a race for stock, where the loser waits for the lock and
then fails a validation instead.

Failures are not cached. A claim whose request ended in an error releases the key, so a client
that hit a validation failure may correct it and retry under the same key. Caching the failure
would make a typo permanent for the length of the retention window, which is a punishment for
the client's mistake that nothing about idempotency asks for.

The key belongs to one account and one request. The stored record carries a fingerprint of the
request body, so a client that reuses a key for a *different* buy-now is told so rather than
being handed the first order as though it had asked for the second. Without the fingerprint,
idempotency would be a way to buy the wrong quantity by accident and be told it was right.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis

from packages.shared.errors import ApiError, ErrorCode

#: How long an answer is kept. A day is long enough for a client to retry after a night of no
#: connectivity and short enough that a key nobody returns to does not live in Redis forever.
IDEMPOTENCY_TTL_SECONDS = 24 * 60 * 60

#: How long a claim survives a process that died holding it. A request that takes a minute is
#: extraordinarily slow, so a claim older than this was not made by a request that is still
#: running, and treating it as abandoned lets the client try again.
CLAIM_TTL_SECONDS = 60

_KEY_PREFIX = "idem"


@dataclass(frozen=True, slots=True)
class ReplayedResponse:
    """An answer that was already given, handed back unchanged."""

    status_code: int
    body: dict[str, Any]


class IdempotencyGuard:
    """The claim one caller holds under one key, and what it may do with it.

    Built by `IdempotencyStore.claim`, which either returns one of these or raises: a caller
    never receives a guard for a key that somebody else has already answered, because that
    caller's answer is the one that stands.
    """

    def __init__(self, redis: Redis, *, account_id: str, key: str, fingerprint: str) -> None:
        self._redis = redis
        self._account_id = account_id
        self._key = key
        self._fingerprint = fingerprint

    @property
    def replay_key(self) -> str:
        """The Redis key this claim lives under."""
        return f"{_KEY_PREFIX}:{self._account_id}:{self._key}"

    async def record(self, status_code: int, body: dict[str, Any]) -> None:
        """Store the answer, so the next request under this key gets it back.

        Written with a plain `SET` rather than `SET NX`: the claim is already ours, and the
        value is the whole point of the exercise.
        """
        payload = json.dumps(
            {
                "state": "answered",
                "fingerprint": self._fingerprint,
                "status": status_code,
                "body": body,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        await self._redis.set(self.replay_key, payload, ex=IDEMPOTENCY_TTL_SECONDS)

    async def release(self) -> None:
        """Give the key back, because this request did not produce an answer worth keeping.

        Only ever called on the failure path. The claim is deleted rather than expired early so
        that a client which hit a validation failure can retry the same corrected request under
        the same key without waiting out `CLAIM_TTL_SECONDS`.
        """
        await self._redis.delete(self.replay_key)


class IdempotencyStore:
    """Hands out claims, and decides who gets the answer that was already given."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def claim(
        self, account_id: str, key: str, fingerprint: str
    ) -> IdempotencyGuard | ReplayedResponse:
        """Take the key, or report the answer that was already stored under it.

        Three answers, and each is a different situation rather than a branch to be exercised:
        a stored answer is a retry, an in-flight claim is a client that tapped twice while the
        first request was still running, and an empty key is the ordinary first attempt.
        """
        guard = IdempotencyGuard(
            self._redis, account_id=account_id, key=key, fingerprint=fingerprint
        )
        claimed = await self._redis.set(
            guard.replay_key, self._in_flight(fingerprint), nx=True, ex=CLAIM_TTL_SECONDS
        )
        if claimed:
            return guard

        stored = await self._redis.get(guard.replay_key)
        if stored is None:
            # The claim expired between the two commands, which means the request that held it
            # died rather than answered. Treating it as a fresh attempt is right: the work that
            # claim was protecting either committed, in which case its key would still be here
            # with an answer, or it did not, in which case nothing happened and this is the
            # first real attempt at it.
            return guard
        # Redis hands back bytes unless the client was told to decode, and it is told in one
        # place and not another, so the value is normalised here rather than assumed.
        text = stored.decode() if isinstance(stored, bytes) else str(stored)
        return self._read(text, key=key, fingerprint=fingerprint)

    def _read(
        self, stored: str, *, key: str, fingerprint: str
    ) -> IdempotencyGuard | ReplayedResponse:
        try:
            record = json.loads(stored)
            state = record["state"]
        except (json.JSONDecodeError, KeyError, TypeError):
            # A value this code did not write. It is treated as a claim somebody still holds
            # rather than as an answer, because handing back a response nobody can account for
            # is worse than making a client try again.
            raise ApiError(
                ErrorCode.CONCURRENT_MODIFICATION,
                details={"reason": "a_request_with_this_idempotency_key_is_still_running"},
            ) from None

        if state != "answered":
            raise ApiError(
                ErrorCode.CONCURRENT_MODIFICATION,
                details={
                    "reason": "a_request_with_this_idempotency_key_is_still_running",
                    "idempotency_key": key,
                },
            )

        if record.get("fingerprint") != fingerprint:
            raise ApiError(
                ErrorCode.VALIDATION_ERROR,
                details={
                    "fields": {
                        "idempotency_key": "already_used_for_a_different_request",
                    }
                },
            )
        return ReplayedResponse(status_code=int(record["status"]), body=dict(record["body"]))

    @staticmethod
    def _in_flight(fingerprint: str) -> str:
        return json.dumps({"state": "in_flight", "fingerprint": fingerprint}, separators=(",", ":"))


def fingerprint_of(*parts: Any) -> str:
    """A stable digest of what a request is asking for.

    Parts are hashed in the order given with their type names attached, so a body of
    `{"quantity_kg": 50}` and one of `{"quantity_kg": "50"}` are different requests - they are
    different requests to the API, and a client that sends both is entitled to be told so. The
    digest is one-way because the point is only to recognise a request again.
    """
    material = json.dumps(parts, separators=(",", ":"), sort_keys=True, default=str)
    return hashlib.sha256(material.encode()).hexdigest()


async def once[T](
    store: IdempotencyStore,
    *,
    account_id: str,
    key: str,
    fingerprint: str,
    work: Callable[[], Awaitable[T]],
    encode: Callable[[T], tuple[int, dict[str, Any]]],
    decode: Callable[[dict[str, Any]], T],
) -> T:
    """Run a mutation once under a key, whatever the client does about sending it again.

    The shape every idempotent endpoint shares, pulled out so that an order, an offer and a
    listing all claim, work, record and release the same way rather than three times by
    resemblance. The caller supplies only what differs: the work, how to turn its result into
    a stored response, and how to turn a stored response back. A failure hands the key straight
    back, because caching a validation failure would make a typo permanent for a day.

    The value returned on a replay is decoded from the stored body, so a second request gets the
    first request's answer rather than a recomputation of it.
    """
    guard = await store.claim(account_id, key, fingerprint)
    if isinstance(guard, ReplayedResponse):
        return decode(guard.body)
    try:
        value = await work()
    except BaseException:
        await guard.release()
        raise
    status_code, body = encode(value)
    await guard.record(status_code, body)
    return value
