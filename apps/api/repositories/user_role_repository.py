"""Reading and writing the extra roles an account may carry.

There is one interesting operation here, and it is inside a conditional insert rather than
a read followed by a write. Granting the agent role twice must leave one row, not two, and
the row that already exists must be *reinstated* rather than duplicated: a role that was
revoked and is granted again is the same pair of (account, role) with a fresh grant date.
Because (user_id, role) is the primary key, the database is what decides, and the writer
asks it to do nothing when the active row is already there.

The reader answers the one question the rest of the platform asks of this table: does this
account hold this role right now.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.models.user_role import Role, UserRole


class UserRoleRepository:
    """The additional roles an account holds."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def has_active_role(self, user_id: uuid.UUID, role: Role) -> bool:
        """Whether this account holds this role right now.

        The active row is the one with no revocation, so a role that was taken away does
        not read as held.
        """
        stmt = select(UserRole.role).where(
            UserRole.user_id == user_id,
            UserRole.role == role,
            UserRole.revoked_at.is_(None),
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none() is not None

    async def grant(self, user_id: uuid.UUID, role: Role) -> bool:
        """Give this account this role, once. Returns whether a change was made.

        Idempotent by construction. When the active row already exists the insert does
        nothing and ``False`` comes back; when the row was revoked, or never existed, the
        insert lands (or re-grants the revoked row) and ``True`` comes back. The single
        statement is the whole guarantee, so two concurrent approvals cannot both believe
        they were the one that granted it.
        """
        stmt = (
            insert(UserRole)
            .values(user_id=user_id, role=role, granted_at=datetime.now(UTC))
            .on_conflict_do_update(
                index_elements=[UserRole.user_id, UserRole.role],
                # Only a revoked row is reinstated. An active one is left exactly as it is,
                # which is what makes "granting again is a no-op" true rather than nearly so.
                set_={"granted_at": datetime.now(UTC), "revoked_at": None},
                where=UserRole.revoked_at.is_not(None),
            )
            .returning(UserRole.user_id)
        )
        result = await self._session.execute(stmt)
        granted = result.scalar_one_or_none() is not None
        if granted:
            await self._session.commit()
        return granted

    async def revoke(self, user_id: uuid.UUID, role: Role) -> bool:
        """Take this role away, once. Returns whether a change was made.

        The update is conditional on the row being active, so revoking an account that does
        not hold the role, or holds it already revoked, is a no-op rather than a second
        revocation date.
        """
        row = await self._session.get(UserRole, (user_id, role))
        if row is None or row.revoked_at is not None:
            return False
        row.revoked_at = datetime.now(UTC)
        await self._session.commit()
        return True
