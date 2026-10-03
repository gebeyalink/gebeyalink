"""The Redis connection.

Verification codes and live sessions both live in Redis rather than the account database:
they expire on their own, they are written far more often than they are read, and a
verification code that outlives its usefulness is a liability rather than a record.

One client per process. Redis multiplexes over a single connection, so pooling clients
would buy nothing and cost memory.
"""

from __future__ import annotations

from redis.asyncio import Redis

from apps.api.core.config import Settings

_client: Redis | None = None


def build_client(settings: Settings) -> Redis:
    """Create a client pointed at the configured Redis."""
    return Redis.from_url(settings.redis_url, decode_responses=True)


def init_client(settings: Settings) -> Redis:
    """Create the client if it does not exist yet and return it."""
    global _client
    if _client is None:
        _client = build_client(settings)
    return _client


def get_client() -> Redis:
    """The client this process uses."""
    if _client is None:
        raise RuntimeError("redis client has not been initialised")
    return _client


async def close_client() -> None:
    """Release the connection, for a clean shutdown or a test teardown."""
    global _client
    if _client is not None:
        await _client.aclose()
    _client = None
