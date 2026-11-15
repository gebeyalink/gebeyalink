"""Reading feature flags, and deciding whether one is on for a particular caller.

The rules are deliberately short enough to state in one place. A missing row is a deployment
defect, not a disabled flag: it raises an internal error naming the flag, because guessing
"off" would make a typo in a flag name silently disable a money gate. A row whose master
switch is off is off for everyone regardless of a percentage or a region.

The two optional scopes are read in order. A percentage rollout, when present, is the whole
answer for that read: the flag is on for a stable bucket of users and not for the rest, and a
region restriction below it is not consulted. When no percentage is configured, a region list
is consulted instead. When neither is configured, an enabled flag is on for everyone.
"""

from __future__ import annotations

import hashlib
from uuid import UUID

from apps.api.models.feature_flag import FeatureFlag
from apps.api.repositories.feature_flag_repository import FeatureFlagRepository
from packages.shared.errors import ApiError, ErrorCode


class FeatureFlagService:
    """The flag table as a read model, with the rollout arithmetic applied."""

    def __init__(self, flags: FeatureFlagRepository) -> None:
        self._flags = flags

    async def is_enabled(
        self,
        name: str,
        *,
        user_id: UUID | str | None = None,
        region_code: str | None = None,
    ) -> bool:
        """Whether ``name`` is on for this caller, refusing when the flag is missing.

        Percentage buckets are computed from the SHA-256 hex digest of the user's UUID in its
        exact string representation: ``str(uuid.UUID("..."))``, lowercase with hyphens. Using
        the canonical string rather than the bytes of the UUID is what makes the bucket stable
        across every caller and every process that sees the same user.
        """
        flag = await self._flags.get(name)
        if flag is None:
            raise ApiError(
                ErrorCode.INTERNAL_ERROR,
                details={"reason": "flag_missing", "flag": name},
            )

        if not flag.enabled:
            return False

        if flag.percentage_rollout is not None:
            # A rollout with no user to bucket cannot include anybody. Failing closed is the
            # only answer that does not widen a partial rollout into a full one.
            if user_id is None:
                return False
            digest = hashlib.sha256(str(user_id).encode("utf-8")).hexdigest()
            bucket = int(digest, 16) % 100
            return bucket < flag.percentage_rollout

        if flag.region_targeting is not None:
            if region_code is None:
                return False
            return region_code in flag.region_targeting

        return True

    async def list_flags(self) -> list[FeatureFlag]:
        """Every installed flag, for the admin editor's read."""
        return await self._flags.list_all()
