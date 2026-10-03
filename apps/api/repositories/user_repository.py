"""Reading and writing accounts.

The repository is the only place that speaks SQL for accounts. Services above it ask
questions in domain terms — "who owns this number", "record their acceptance" — and never
learn whether the answer came from a cache, a join, or a scan.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.models.user import User


class UserRepository:
    """Account reads and writes."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_by_phone(self, phone_e164: str) -> User | None:
        """The account holding this number, if any.

        Used to decide between creating an account and signing an existing one back in.
        """
        result = await self._session.execute(select(User).where(User.phone_e164 == phone_e164))
        return result.scalar_one_or_none()

    async def add(self, user: User) -> User:
        """Create the account.

        The write is committed here rather than left to the request, because the account
        has to outlive the response that announced it: the session tokens issued on the way
        out refer to a row that must still be there when they are used.
        """
        self._session.add(user)
        await self._session.commit()
        return user

    async def record_terms_acceptance(
        self,
        user: User,
        terms_version: str,
        accepted_at: datetime,
    ) -> None:
        """Write down which version of the terms this person accepted, and when.

        Recorded on every verified sign-in rather than only the first: a new version of the
        terms means the acceptance must be re-earned, and the newest acceptance is the one
        that authorises trading.
        """
        user.terms_version = terms_version
        user.terms_accepted_at = accepted_at
        await self._session.commit()

    async def save(self, user: User) -> None:
        """Persist a change to an account that already exists."""
        await self._session.commit()
