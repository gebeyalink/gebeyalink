"""Reading and writing the dated fee policy.

The active policy is defined by time, not by a flag: a row is current when its window contains
the instant being asked about and ``effective_to`` is null for the row that is open now. Reads
order by ``effective_from`` descending so that a backfilled row cannot shadow the later one.

``current`` returns the pure :class:`packages.shared.money.FeeConfig` value the arithmetic
expects. ``current_row`` exists for the two callers that need the row itself: the API's read
model, which reports when the policy took effect, and the approver, which closes it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.models.fee_config import FeeConfig as FeeConfigRow
from packages.shared.errors import ApiError, ErrorCode
from packages.shared.money import FeeConfig


class FeeConfigRepository:
    """The fee policy as of an instant."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def current(self, *, now: datetime | None = None) -> FeeConfig:
        """The policy in force, or an internal error if the deployment has none.

        There is deliberately no fallback to constants. A process that cannot find the active
        row cannot know what price it is charging, and guessing would put an order on the books
        under a policy nobody approved.
        """
        row = await self.current_row(now=now)
        if row is None:
            raise ApiError(
                ErrorCode.INTERNAL_ERROR,
                details={"reason": "fee_config_missing"},
            )
        return to_fee_config(row)

    async def current_row(
        self,
        *,
        now: datetime | None = None,
        for_update: bool = False,
    ) -> FeeConfigRow | None:
        """The active row, optionally locked for the approval's close-and-open transaction."""
        moment = now or datetime.now(UTC)
        statement = (
            select(FeeConfigRow)
            .where(
                FeeConfigRow.effective_from <= moment,
                or_(
                    FeeConfigRow.effective_to.is_(None),
                    FeeConfigRow.effective_to > moment,
                ),
            )
            .order_by(FeeConfigRow.effective_from.desc(), FeeConfigRow.created_at.desc())
            .limit(1)
        )
        if for_update:
            statement = statement.with_for_update()
        return await self._session.scalar(statement)

    async def close(self, row: FeeConfigRow, *, at: datetime) -> None:
        """End a policy's window at ``at`` without changing any of its numbers."""
        row.effective_to = at
        await self._session.flush()

    async def add(
        self,
        *,
        config: FeeConfig,
        effective_from: datetime,
        created_by: uuid.UUID,
        reason: str,
    ) -> FeeConfigRow:
        """Open a new policy window. The caller has already closed the previous one."""
        row = FeeConfigRow(
            fee_payer=config.fee_payer,
            platform_fee_bps=config.platform_fee_bps,
            min_fee_santim=config.min_fee_santim,
            max_fee_santim=config.max_fee_santim,
            vat_bps=config.vat_bps,
            effective_from=effective_from,
            effective_to=None,
            created_by=created_by,
            change_reason=reason,
        )
        self._session.add(row)
        await self._session.flush()
        return row


def to_fee_config(row: FeeConfigRow) -> FeeConfig:
    """Copy the stored columns into the arithmetic's immutable value."""
    return FeeConfig(
        fee_payer=row.fee_payer,
        platform_fee_bps=row.platform_fee_bps,
        min_fee_santim=row.min_fee_santim,
        max_fee_santim=row.max_fee_santim,
        vat_bps=row.vat_bps,
    )
