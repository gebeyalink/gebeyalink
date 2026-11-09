"""Reading and writing accounts.

The repository is the only place that speaks SQL for accounts. Services above it ask
questions in domain terms — "who owns this number", "record their acceptance" — and never
learn whether the answer came from a cache, a join, or a scan.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.models.user import AccountStatus, User
from packages.shared.i18n.languages import Language


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

    async def find_by_id(self, user_id: uuid.UUID) -> User | None:
        """The account with this identifier, if it still exists."""
        result = await self._session.execute(select(User).where(User.id == user_id))
        return result.scalar_one_or_none()

    async def page_for_admin(
        self,
        *,
        phone_prefix: str | None,
        status: AccountStatus | None,
        after: tuple[datetime, uuid.UUID] | None,
        limit: int,
    ) -> list[User]:
        """One page of the staff console's account search, newest first.

        The phone filter is a prefix match. A full E.164 number is therefore an exact search,
        and a partial number is the "I only caught the first digits" search support actually
        makes. ``autoescape`` keeps a caller's ``%`` or ``_`` from turning into a wildcard.
        """
        stmt = select(User)
        if phone_prefix is not None:
            stmt = stmt.where(User.phone_e164.startswith(phone_prefix, autoescape=True))
        if status is not None:
            stmt = stmt.where(User.status == status)
        if after is not None:
            stmt = stmt.where(tuple_(User.created_at, User.id) < after)
        stmt = stmt.order_by(User.created_at.desc(), User.id.desc()).limit(limit + 1)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def refresh(self, user: User) -> None:
        """Re-read a row after a commit expired its server-generated columns.

        A response is built synchronously after the route's final await, so an expired
        ``updated_at`` cannot be lazy-loaded there. The explicit refresh moves that read back
        under an await.
        """
        await self._session.refresh(user)

    async def save_language(self, user: User, language: Language) -> User:
        """Record the language this trader reads in.

        An explicit choice in a request is worth keeping; a language the caller merely
        happened to read the response in is not, which is why nothing here is inferred from
        a header.
        """
        user.language = language
        await self._session.flush()
        return user

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
