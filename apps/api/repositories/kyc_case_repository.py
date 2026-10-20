"""Reading and writing the tier upgrades a trader has asked for.

A case is read by its own identity and by the account that owns it. There is no read-by-tier
or read-by-document here: what exists is what the submission path needs — write one, ask
whether this account already has one waiting, and read one back — plus the by-id read a
reviewer's route will use, which is included now so the admin slice has the entry point rather
than reaching past this class for it.

The one question worth stating is ``find_pending_by_user``. A trader may hold at most one
*waiting* case, so the submission path asks this first and refuses with a conflict if it
answers. A decided case — approved or rejected — is not returned, because a decided case is
history and does not block a fresh submission.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.models.kyc_case import KycCase, KycStatus, KycTier


class KycCaseRepository:
    """The tier-upgrade cases an account has submitted, and their state."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @property
    def session(self) -> AsyncSession:
        """The session this repository writes on.

        Exposed because the reviewer's approve and reject both mutate a case and write an audit
        row in one transaction, and the audit repository must share the very same session. A
        service that reached for its own session could land a decision without its record.
        """
        return self._session

    async def add(self, case: KycCase) -> KycCase:
        """Add a case and give it its identity, without making the write permanent.

        Flushed and not committed: a submission may also carry a tax number onto the profile,
        and the caller wants the case and that write to land together or not at all.
        """
        self._session.add(case)
        await self._session.flush()
        return case

    async def find_by_id(self, case_id: uuid.UUID) -> KycCase | None:
        """One case by its own identity, whoever owns it.

        Nothing about who may read this is decided here; a reviewer's route applies its own
        rules about which cases a staff account may see.
        """
        stmt = select(KycCase).where(KycCase.id == case_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def find_pending_by_user(self, user_id: uuid.UUID) -> KycCase | None:
        """This account's waiting case, if it has one.

        The one row that blocks a second submission. A decided case is not returned, because
        a trader who was rejected may ask again and one who was approved may ask for the next
        tier up.
        """
        stmt = select(KycCase).where(
            KycCase.user_id == user_id,
            KycCase.status == KycStatus.PENDING,
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def commit(self) -> None:
        await self._session.commit()

    async def page_queue(
        self,
        *,
        status: KycStatus | None,
        tier_requested: KycTier | None,
        after: tuple[datetime, uuid.UUID] | None,
        limit: int,
    ) -> list[KycCase]:
        """One page of the reviewer's queue, newest first.

        Ordered by ``created_at`` with the id as a tiebreak, because two cases can share a
        timestamp and a keyset cursor over a non-unique key skips rows. The filter is applied
        to the same columns the order uses, so the seek and the sort agree.
        """
        stmt = select(KycCase)
        if status is not None:
            stmt = stmt.where(KycCase.status == status)
        if tier_requested is not None:
            stmt = stmt.where(KycCase.tier_requested == tier_requested)
        if after is not None:
            created_at, case_id = after
            stmt = stmt.where(
                or_(
                    KycCase.created_at < created_at,
                    and_(KycCase.created_at == created_at, KycCase.id < case_id),
                )
            )
        stmt = stmt.order_by(KycCase.created_at.desc(), KycCase.id.desc()).limit(limit + 1)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())
