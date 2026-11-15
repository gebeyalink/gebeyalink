"""Reading and writing the feature-flag rows.

The repository is intentionally small and has one write path: ``upsert``. Reads are by name or
the whole table; a change arrives through the maker/checker applier, which decides the values
and asks this repository to make them so. No route writes a flag directly, and no other service
does either.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.models.feature_flag import FeatureFlag


class FeatureFlagRepository:
    """The flag table, as the services that read and apply changes see it."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, name: str) -> FeatureFlag | None:
        """One flag by its stable name, or none when the deployment has not installed it."""
        return await self._session.scalar(select(FeatureFlag).where(FeatureFlag.name == name))

    async def list_all(self) -> list[FeatureFlag]:
        """Every flag, ordered by name so the editor's list does not move between reads."""
        result = await self._session.execute(select(FeatureFlag).order_by(FeatureFlag.name.asc()))
        return list(result.scalars().all())

    async def upsert(
        self,
        name: str,
        *,
        enabled: bool,
        percentage_rollout: int | None,
        region_targeting: list[str] | None,
    ) -> FeatureFlag:
        """Apply an approved change, creating the row if the deployment lacks it.

        Only the maker/checker applier calls this. The caller has already validated the values
        and holds the proposal's row lock; this method's job is the write and the flush that
        makes a constraint violation part of the same transaction.
        """
        row = await self.get(name)
        if row is None:
            row = FeatureFlag(
                name=name,
                enabled=enabled,
                percentage_rollout=percentage_rollout,
                region_targeting=region_targeting,
            )
            self._session.add(row)
        else:
            row.enabled = enabled
            row.percentage_rollout = percentage_rollout
            row.region_targeting = region_targeting
        await self._session.flush()
        return row
