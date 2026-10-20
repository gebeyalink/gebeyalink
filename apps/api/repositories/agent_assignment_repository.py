"""Reading and writing the relationships between agents and the farmers they carry.

Every question here is asked of the *active* relationship — the one whose ``revoked_at`` is
null and whose ``expires_at`` is still ahead — because that is the only relationship that
lets an agent act. A revoked or lapsed assignment is history: kept, readable, and never a
reason to permit anything.

Two of the operations are deliberately compare-and-set shaped rather than read-then-write.
Onboarding a farmer an agent already carries must refresh the one active row rather than
produce a second, so the writer looks for the active row first and either updates its end
date or inserts a new one; revoking is an update of the revocation column conditional on it
being null, so two revocations cannot both stamp a date.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.models.agent_assignment import AgentAssignment

#: How long an assignment stands before the farmer must confirm it again. FR-1707's
#: twelve-month renewal. Not a settings field: it is a requirement, not a deployment choice,
#: and a value that could be configured is a value two deployments would disagree on.
ASSIGNMENT_TERM = timedelta(days=365)


def assignment_expiry(created_at: datetime) -> datetime:
    """The moment an assignment made now lapses: twelve months on."""
    return created_at + ASSIGNMENT_TERM


class AgentAssignmentRepository:
    """The farmers each agent carries, and the active window of each relationship."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        user_id: uuid.UUID,
        agent_id: uuid.UUID,
        expires_at: datetime,
    ) -> AgentAssignment:
        """Record a new relationship, without making the write permanent.

        Flushed and not committed: the caller is mid-transaction — a farmer's account may be
        being created in the same breath — and committing here would land half of that.
        """
        assignment = AgentAssignment(user_id=user_id, agent_id=agent_id, expires_at=expires_at)
        self._session.add(assignment)
        await self._session.flush()
        return assignment

    async def find_active_by_agent(
        self, agent_id: uuid.UUID, *, now: datetime | None = None
    ) -> list[AgentAssignment]:
        """Every farmer this agent currently carries."""
        moment = now or datetime.now(UTC)
        stmt = (
            select(AgentAssignment)
            .where(
                AgentAssignment.agent_id == agent_id,
                AgentAssignment.revoked_at.is_(None),
                AgentAssignment.expires_at > moment,
            )
            .order_by(AgentAssignment.created_at)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def find_active_by_farmer(
        self, user_id: uuid.UUID, *, now: datetime | None = None
    ) -> list[AgentAssignment]:
        """Every agent currently carrying this farmer."""
        moment = now or datetime.now(UTC)
        stmt = (
            select(AgentAssignment)
            .where(
                AgentAssignment.user_id == user_id,
                AgentAssignment.revoked_at.is_(None),
                AgentAssignment.expires_at > moment,
            )
            .order_by(AgentAssignment.created_at)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def find_active_pair(
        self,
        *,
        user_id: uuid.UUID,
        agent_id: uuid.UUID,
        now: datetime | None = None,
    ) -> AgentAssignment | None:
        """The live relationship between this farmer and this agent, if there is one.

        This is the single question onboarding and delegated listing both ask, so it lives
        here rather than being spelled out in each caller.
        """
        moment = now or datetime.now(UTC)
        stmt = select(AgentAssignment).where(
            AgentAssignment.user_id == user_id,
            AgentAssignment.agent_id == agent_id,
            AgentAssignment.revoked_at.is_(None),
            AgentAssignment.expires_at > moment,
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def refresh(self, assignment: AgentAssignment, *, now: datetime | None = None) -> None:
        """Push an active assignment's end date out by another full term.

        Used when the same farmer is onboarded again: a re-confirmation is the farmer's own
        act and FR-1707 says that is what renews the relationship, so the one active row is
        given a fresh twelve months rather than a second row being written.
        """
        moment = now or datetime.now(UTC)
        assignment.expires_at = assignment_expiry(moment)
        await self._session.flush()

    async def revoke(self, *, agent_id: uuid.UUID, now: datetime | None = None) -> int:
        """End every active relationship this agent holds. Returns how many were ended.

        Conditional on the relationship still being active, so a second revocation is a
        no-op that reports zero rather than stamping a second date onto rows that already
        carry one. Committed here, because revocation is its own act and not part of another.
        """
        moment = now or datetime.now(UTC)
        stmt = (
            update(AgentAssignment)
            .where(
                AgentAssignment.agent_id == agent_id,
                AgentAssignment.revoked_at.is_(None),
            )
            .values(revoked_at=moment)
            .returning(AgentAssignment.id)
        )
        result = await self._session.execute(stmt)
        revoked = len(list(result.scalars().all()))
        await self._session.commit()
        return revoked

    async def list_expiring(
        self, *, before: datetime, now: datetime | None = None
    ) -> list[AgentAssignment]:
        """Active assignments that lapse at or before a moment.

        The renewal prompt's raw material: the farmers whose relationship with an agent is
        about to need re-confirming. Active means unrevoked and not already expired.
        """
        moment = now or datetime.now(UTC)
        stmt = (
            select(AgentAssignment)
            .where(
                AgentAssignment.revoked_at.is_(None),
                AgentAssignment.expires_at > moment,
                AgentAssignment.expires_at <= before,
            )
            .order_by(AgentAssignment.expires_at)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def commit(self) -> None:
        await self._session.commit()
