"""Where live sessions live.

A session is the record that a refresh token is still the current one. Keeping it lets us
answer a question the token itself cannot: "is this the token I issued, or an older one
somebody kept?" That question is the whole point — a refresh token that can be replayed
is a standing key to somebody's account, so an old token coming back means either a
thief or a confused client, and in both cases the safe answer is to close every door.

Sessions are indexed per account as well as per token, because revoking everything at
once has to be possible when replay is detected.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from redis.asyncio import Redis
from redis.typing import EncodableT


def _as_text(value: EncodableT) -> str:
    """Read a stored value as text whichever way the client is configured to return it."""
    return value.decode() if isinstance(value, bytes) else str(value)


@dataclass(frozen=True, slots=True)
class SessionRecord:
    """One live session and the token currently expected to rotate it."""

    session_id: str
    user_id: uuid.UUID
    refresh_token_hash: str
    expires_at: datetime


class SessionRepository:
    """Stores sessions and the index of each account's live sessions."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    @staticmethod
    def _session_key(session_id: str) -> str:
        return f"session:{session_id}"

    @staticmethod
    def _account_index_key(user_id: uuid.UUID) -> str:
        return f"account-sessions:{user_id}"

    async def open(
        self,
        session_id: str,
        user_id: uuid.UUID,
        refresh_token_hash: str,
        expires_at: datetime,
        ttl_seconds: int,
    ) -> None:
        """Record a new session, or the rotated state of an existing one."""
        key = self._session_key(session_id)
        await self._redis.hset(
            key,
            mapping={
                "session_id": session_id,
                "user_id": str(user_id),
                "refresh_token_hash": refresh_token_hash,
                "expires_at": expires_at.isoformat(),
            },
        )
        await self._redis.expire(key, ttl_seconds)
        index_key = self._account_index_key(user_id)
        await self._redis.sadd(index_key, session_id)
        await self._redis.expire(index_key, ttl_seconds)

    async def get(self, session_id: str) -> SessionRecord | None:
        """The session behind a refresh token, or nothing if it is gone or expired."""
        data = await self._redis.hgetall(self._session_key(session_id))
        if not data:
            return None
        try:
            # Redis hands back bytes unless the client was told to decode; normalising here
            # keeps that detail in one place instead of at every call site.
            fields = {name: _as_text(value) for name, value in data.items()}
            return SessionRecord(
                session_id=fields["session_id"],
                user_id=uuid.UUID(fields["user_id"]),
                refresh_token_hash=fields["refresh_token_hash"],
                expires_at=datetime.fromisoformat(fields["expires_at"]),
            )
        except (KeyError, ValueError):  # pragma: no cover - corrupted row
            return None

    async def rotate(
        self,
        session_id: str,
        user_id: uuid.UUID,
        refresh_token_hash: str,
        expires_at: datetime,
        ttl_seconds: int,
    ) -> None:
        """Make a freshly issued refresh token the only one this session accepts.

        The previous token is overwritten rather than kept, which is what makes it
        single-use: the next time it is presented, the digests disagree.
        """
        await self.open(
            session_id=session_id,
            user_id=user_id,
            refresh_token_hash=refresh_token_hash,
            expires_at=expires_at,
            ttl_seconds=ttl_seconds,
        )

    async def close(self, session_id: str) -> None:
        """End one session, e.g. when its owner signs out."""
        record = await self.get(session_id)
        await self._redis.delete(self._session_key(session_id))
        if record is not None:
            await self._redis.srem(self._account_index_key(record.user_id), session_id)

    async def account_session_ids(self, user_id: uuid.UUID) -> set[str]:
        """Every session currently open for an account."""
        members = await self._redis.smembers(self._account_index_key(user_id))
        return {str(member) for member in members}

    async def close_every_session_for(self, user_id: uuid.UUID) -> int:
        """End all of an account's sessions at once and report how many were closed.

        This is the response to a replayed refresh token. Closing only the offending
        session would leave the attacker holding a valid token for any other session they
        had already rotated, so every door closes.
        """
        session_ids = await self.account_session_ids(user_id)
        if not session_ids:
            return 0
        keys = [self._session_key(session_id) for session_id in session_ids]
        await self._redis.delete(*keys)
        await self._redis.delete(self._account_index_key(user_id))
        return len(session_ids)
