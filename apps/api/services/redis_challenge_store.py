"""The short-lived handles the agent onboarding transfer passes between its two requests.

Onboarding is two requests: the first sends a code to the farmer's phone, the second brings
that code back. Something has to connect them, and what it must hold is only *which pair of
people* the exchange is about — the agent and the farmer's number. It must not hold the
farmer's account, because none exists yet, and it must not hold the code, because a code in
a store readable by whoever holds the store is a code in the wrong place.

The handle is a random string handed to the caller; what is written down is its SHA-256, in
the same spirit as the action tokens, so a copy of the store is a copy of digests rather
than of live handles. It is single-use and short-lived: consuming it deletes it, so a second
confirmation cannot spend the same handle again, and it is gone when the code it accompanies
expires.

The pair is bound into the record and checked on the way out. A handle minted by one agent
cannot be handed to another, so one agent cannot complete an onboarding another agent began.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from dataclasses import dataclass

from redis.asyncio import Redis

from packages.shared.errors import ApiError, ErrorCode

#: The key prefix, so the handles can be scanned or evicted as one kind of thing.
CHALLENGE_KEY_PREFIX = "agent-onboard-challenge"


@dataclass(frozen=True, slots=True)
class OnboardingChallenge:
    """The pair an onboarding handle is about."""

    agent_id: uuid.UUID
    phone_e164: str


def _digest(token: str) -> str:
    """The stored form of a handle: its SHA-256, never the handle itself."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class AgentChallengeRepository:
    """Minting, reading and spending the onboarding pair in Redis."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    @staticmethod
    def _key(token: str) -> str:
        return f"{CHALLENGE_KEY_PREFIX}:{_digest(token)}"

    async def mint(self, *, agent_id: uuid.UUID, phone_e164: str, ttl_seconds: int) -> str:
        """Write a fresh handle for this pair and return it. One handle, one pair."""
        # 32 bytes of entropy, URL-safe: a handle nobody can guess and nobody has to type.
        token = secrets.token_urlsafe(32)
        payload = json.dumps({"agent_id": str(agent_id), "phone": phone_e164})
        await self._redis.set(self._key(token), payload, ex=ttl_seconds)
        return token

    async def consume(self, token: str, *, agent_id: uuid.UUID) -> OnboardingChallenge:
        """Read a handle and spend it in one breath, or say why it cannot be spent.

        ``getdel`` is the whole of the single-use guarantee: the read and the delete are one
        operation, so two simultaneous confirmations cannot both find the handle present.
        The agent bound into the record is checked against the caller, so a handle minted by
        one agent is refused to another even if it leaks.
        """
        if not token:
            raise ApiError(ErrorCode.VALIDATION_ERROR, details={"reason": "challenge_missing"})
        raw = await self._redis.getdel(self._key(token))
        if raw is None:
            raise ApiError(ErrorCode.OTP_EXPIRED, details={"reason": "challenge_expired"})
        text = raw.decode() if isinstance(raw, bytes) else str(raw)
        payload = json.loads(text)
        if str(payload.get("agent_id")) != str(agent_id):
            raise ApiError(ErrorCode.FORBIDDEN, details={"reason": "challenge_agent_mismatch"})
        return OnboardingChallenge(
            agent_id=agent_id,
            phone_e164=str(payload["phone"]),
        )
