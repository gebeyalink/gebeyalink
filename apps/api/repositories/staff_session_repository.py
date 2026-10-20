"""Reading and writing the sessions a staff sign-in opens.

Three questions this table answers, and they are the three a session's life asks: is this
session live right now (the dependency), end this one (logout), and end every live session this
staff member holds except the one making the request (a password or role change). The last is
the one worth stating: revoking *all but the current* is what lets a password change keep the
session that performed it while ending every other place the old password was still signed in.

"Live" means one thing here and it is spelled out once: not revoked, and not past its expiry.
Every read below uses the same predicate, so a session cannot be live to one caller and ended to
another.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.models.staff_session import StaffSession


class StaffSessionRepository:
    """The staff sign-ins that are open, and ending them."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        staff_user_id: uuid.UUID,
        expires_at: datetime,
        ip: str | None = None,
    ) -> StaffSession:
        """Open a session, flushed so its id exists for the token that names it."""
        session = StaffSession(
            staff_user_id=staff_user_id,
            expires_at=expires_at,
            ip=ip,
        )
        self._session.add(session)
        await self._session.flush()
        return session

    async def find_live(
        self, session_id: uuid.UUID, *, now: datetime | None = None
    ) -> StaffSession | None:
        """The session with this id if it is still live, else None.

        Both conditions are checked here rather than left to the token: the token's ``exp`` is
        checked as well, but this row is the authority, so a verifier whose clock runs fast
        cannot hold a session open past what the database says.
        """
        moment = now or datetime.now(UTC)
        result = await self._session.execute(
            select(StaffSession).where(
                StaffSession.id == session_id,
                StaffSession.revoked_at.is_(None),
                StaffSession.expires_at > moment,
            )
        )
        return result.scalar_one_or_none()

    async def revoke(self, session_id: uuid.UUID, *, now: datetime | None = None) -> bool:
        """End one session. Returns whether it was live to end.

        An update, never a delete: a revoked session is what a "this token was used after it was
        ended" question is answered from.
        """
        moment = now or datetime.now(UTC)
        result = await self._session.execute(
            update(StaffSession)
            .where(StaffSession.id == session_id, StaffSession.revoked_at.is_(None))
            .values(revoked_at=moment)
            .returning(StaffSession.id)
        )
        revoked = result.scalar_one_or_none() is not None
        await self._session.flush()
        return revoked

    async def revoke_all_for_staff(
        self,
        staff_user_id: uuid.UUID,
        *,
        except_session_id: uuid.UUID | None = None,
        now: datetime | None = None,
    ) -> int:
        """End every live session this staff member holds, except one. Returns how many ended.

        ``except_session_id`` is the session performing a password change: it stays live, while
        every *other* place the old credential was signed in is ended. Left as None this ends
        them all, which is what a role change or a disablement does.
        """
        moment = now or datetime.now(UTC)
        stmt = (
            update(StaffSession)
            .where(
                StaffSession.staff_user_id == staff_user_id,
                StaffSession.revoked_at.is_(None),
                StaffSession.expires_at > moment,
            )
            .values(revoked_at=moment)
            .returning(StaffSession.id)
        )
        if except_session_id is not None:
            stmt = stmt.where(StaffSession.id != except_session_id)
        result = await self._session.execute(stmt)
        ended = len(list(result.scalars().all()))
        await self._session.flush()
        return ended

    async def commit(self) -> None:
        await self._session.commit()
