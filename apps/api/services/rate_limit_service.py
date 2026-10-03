"""Abuse controls keyed to whoever is calling.

Sign-in endpoints are the most attractive thing on a public API to hammer: no account is
needed, every attempt is cheap for the caller, and success hands over an account. The
limits therefore bind to the caller's address rather than to an account, because at the
moment the limit matters most there is no account to count against.

Two different shapes of abuse need two different ceilings. A per-minute ceiling keeps a
single address from hammering the endpoint at all. A per-day ceiling on codes sent — counted
both per number and per address — is what stops one host from spraying codes across many
numbers to burn through other people's daily allowance.
"""

from __future__ import annotations

from dataclasses import dataclass

from redis.asyncio import Redis

SECONDS_PER_MINUTE = 60
SECONDS_PER_DAY = 24 * 60 * 60


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    """Whether a call may proceed, and what to tell the caller if it may not."""

    allowed: bool
    limit: int
    remaining: int
    retry_after_seconds: int

    def headers(self) -> dict[str, str]:
        """Quota headers a client uses to pace itself instead of guessing."""
        return {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(max(0, self.remaining)),
            "Retry-After": str(self.retry_after_seconds),
        }


class RateLimitService:
    """Counts calls per address and per number against the platform's ceilings."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    @staticmethod
    def _minute_key(client_ip: str) -> str:
        return f"rate-auth-minute:{client_ip}"

    async def check_sign_in_calls(self, client_ip: str, limit: int) -> RateLimitDecision:
        """Allow up to ``limit`` sign-in calls per minute from one address."""
        key = self._minute_key(client_ip)
        used = int(await self._redis.incr(key))
        if used == 1:
            await self._redis.expire(key, SECONDS_PER_MINUTE)
        if used > limit:
            ttl = await self._redis.ttl(key)
            return RateLimitDecision(
                allowed=False,
                limit=limit,
                remaining=0,
                retry_after_seconds=max(1, int(ttl) if ttl and ttl > 0 else SECONDS_PER_MINUTE),
            )
        return RateLimitDecision(
            allowed=True,
            limit=limit,
            remaining=limit - used,
            retry_after_seconds=0,
        )

    @staticmethod
    def _codes_per_number_key(phone_e164: str) -> str:
        return f"rate-codes-day-phone:{phone_e164}"

    @staticmethod
    def _codes_per_address_key(client_ip: str) -> str:
        return f"rate-codes-day-address:{client_ip}"

    async def check_codes_to_number(self, phone_e164: str, limit: int) -> RateLimitDecision:
        """Allow up to ``limit`` codes per number per day."""
        return await self._check_daily(self._codes_per_number_key(phone_e164), limit)

    async def check_codes_from_address(self, client_ip: str, limit: int) -> RateLimitDecision:
        """Allow up to ``limit`` codes per address per day, across every number."""
        return await self._check_daily(self._codes_per_address_key(client_ip), limit)

    async def _check_daily(self, key: str, limit: int) -> RateLimitDecision:
        used = int(await self._redis.get(key) or 0)
        if used >= limit:
            ttl = await self._redis.ttl(key)
            return RateLimitDecision(
                allowed=False,
                limit=limit,
                remaining=0,
                retry_after_seconds=max(1, int(ttl) if ttl and ttl > 0 else SECONDS_PER_DAY),
            )
        return RateLimitDecision(
            allowed=True, limit=limit, remaining=limit - used, retry_after_seconds=0
        )

    async def record_code_sent(self, phone_e164: str, client_ip: str, ttl_seconds: int) -> None:
        """Count a code against both daily ceilings once it has actually gone out.

        Counted after the send rather than before it, so a failure in the messaging backend
        does not consume somebody's daily allowance.
        """
        for key in (self._codes_per_number_key(phone_e164), self._codes_per_address_key(client_ip)):
            if int(await self._redis.incr(key)) == 1:
                await self._redis.expire(key, ttl_seconds)
