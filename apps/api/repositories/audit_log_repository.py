"""Writing the record of what was done.

One operation: append a row. There is no update and no delete here, and that is not an
omission — the table refuses both at the database, and a repository method for either would be
a door that opens onto a wall. What this module adds on top of the plain insert is the one thing
every caller needs and none should have to remember: turning a before/after pair of states into
the two SHA-256 hashes the columns hold.

The hash is over the *canonical* JSON of the state — keys sorted, no insignificant whitespace —
so two equal states always hash the same and a diff is never an artefact of key order. A state
that is ``None`` on one side (a creation has no before, a removal has no after) hashes to null,
which is different from the hash of the empty state and is not confused with it.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.models.audit_log import AuditLog


class AuditLogRepository:
    """The append-only log, written one row at a time."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(
        self,
        *,
        actor_id: uuid.UUID | None,
        actor_role: str,
        action: str,
        target_type: str,
        target_id: uuid.UUID | None = None,
        before: Any = None,
        after: Any = None,
        ip: str | None = None,
    ) -> AuditLog:
        """Append one row, flushed but not committed.

        Flushed rather than committed so the audit lands in the *same* transaction as the action
        it records: an approval and its audit row succeed or fail together. A committed-here
        audit could describe an action that then rolled back, which is worse than no audit.
        """
        row = AuditLog(
            actor_id=actor_id,
            actor_role=actor_role,
            action=action,
            target_type=target_type,
            target_id=target_id,
            before_hash=state_hash(before),
            after_hash=state_hash(after),
            ip=ip,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def commit(self) -> None:
        """Make the appended rows permanent.

        Separate from ``record`` because the audit row is written where the action happens, and
        the caller owns the transaction boundary: the two must share it, and a ``record`` that
        committed on its own could describe an action that then rolled back.
        """
        await self._session.commit()


def state_hash(state: Any) -> str | None:
    """SHA-256 hex of a state's canonical JSON, or None when there is no state.

    ``None`` means "this side does not exist": a creation has no before, a deletion has no
    after. It is deliberately distinct from the hash of ``{}``.
    """
    if state is None:
        return None
    canonical = json.dumps(state, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
