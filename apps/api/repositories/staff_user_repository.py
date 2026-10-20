"""Reading and writing staff accounts.

A staff account is looked up one of two ways: by its email, which is how a sign-in finds it, and
by its id, which is how everything else does. There is no list route here yet — a "read all
staff" belongs with the console that needs it, and nothing in this slice reads across accounts.

The writes are the account's own lifecycle: create, record a sign-in, set a password, change a
role, and disable. Each is small and none of them reaches past this table; the rules about *who*
may call them live in the service above.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.models.staff_user import StaffRole, StaffUser


class StaffUserRepository:
    """Staff accounts and the changes to one."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_by_email(self, email: str) -> StaffUser | None:
        """The account with this address, if one exists.

        Compared against the stored (lower-cased) value; the request shape lower-cases before it
        reaches here, so an address is one address however it was typed.
        """
        result = await self._session.execute(
            select(StaffUser).where(StaffUser.email == email.lower())
        )
        return result.scalar_one_or_none()

    async def find_by_id(self, staff_user_id: uuid.UUID) -> StaffUser | None:
        """The account with this identifier."""
        result = await self._session.execute(select(StaffUser).where(StaffUser.id == staff_user_id))
        return result.scalar_one_or_none()

    async def add(self, staff: StaffUser) -> StaffUser:
        """Create the account, flushed so it has its identity before a session names it."""
        self._session.add(staff)
        await self._session.flush()
        return staff

    async def note_login(self, staff: StaffUser, *, at: datetime | None = None) -> None:
        """Record that this account just signed in."""
        staff.last_login_at = at or datetime.now(UTC)
        await self._session.flush()

    async def set_password(
        self, staff: StaffUser, password_hash: str, *, must_change: bool
    ) -> None:
        """Write a new password hash and whether the member must change it."""
        staff.password_hash = password_hash
        staff.must_change_password = must_change
        await self._session.flush()

    async def set_role(self, staff: StaffUser, role: StaffRole) -> None:
        """Move the account to a new role."""
        staff.role = role
        await self._session.flush()

    async def set_disabled(self, staff: StaffUser, *, disabled_at: datetime | None) -> None:
        """Disable the account (a moment) or enable it (None)."""
        staff.disabled_at = disabled_at
        await self._session.flush()

    async def set_totp_secret(self, staff: StaffUser, encrypted_secret: str) -> None:
        """Replace the stored TOTP secret, as a reset does."""
        staff.totp_secret_encrypted = encrypted_secret
        await self._session.flush()

    async def commit(self) -> None:
        await self._session.commit()
