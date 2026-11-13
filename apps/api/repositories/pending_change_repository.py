"""The maker/checker queue.

The repository is deliberately small. It knows how to append a proposal, read one under a row
lock, list what is waiting, and commit the transaction the service owns. It does not know what
any payload means: applying a fee change belongs to the fee applier, and applying a feature flag
change will belong to a different one.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.models.pending_change import PendingChange, PendingChangeStatus


class PendingChangeRepository:
    """The queue's rows and the lock an approval needs."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(
        self,
        *,
        kind: str,
        payload: dict[str, Any],
        submitted_by: uuid.UUID,
    ) -> PendingChange:
        """Append a proposal in the caller's transaction."""
        row = PendingChange(
            kind=kind,
            status=PendingChangeStatus.PENDING,
            payload=payload,
            submitted_by=submitted_by,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def find_for_update(self, change_id: uuid.UUID) -> PendingChange | None:
        """Read one proposal under a row lock, so two approvals cannot both decide it."""
        statement = select(PendingChange).where(PendingChange.id == change_id).with_for_update()
        return await self._session.scalar(statement)

    async def pending(self, *, limit: int = 100) -> list[PendingChange]:
        """The waiting proposals, oldest first, for the console's review list."""
        statement = (
            select(PendingChange)
            .where(PendingChange.status == PendingChangeStatus.PENDING)
            .order_by(PendingChange.created_at.asc(), PendingChange.id.asc())
            .limit(limit)
        )
        result = await self._session.execute(statement)
        return list(result.scalars().all())

    async def commit(self) -> None:
        """Make the proposal and everything applied beside it permanent together."""
        await self._session.commit()
