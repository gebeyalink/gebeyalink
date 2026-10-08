"""Reading and spending action tokens.

There is one interesting operation here, and it is a compare-and-set rather than a read
followed by a write. Consuming a token has to be atomic: two confirmations of the same
delivery arriving together must not both find the token unused and both proceed. The update
is therefore conditional on the token still being unspent and unexpired, and it is the
database that decides which of the two callers won.

The reason a consume failed is kept apart rather than collapsed into a boolean: not-found,
expired and already-used are three different things to the person reading a support ticket,
even though the trader who sent the command is told the same sentence for all three.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.models.action_token import ActionToken


class ConsumeReason(StrEnum):
    """Why consuming a token did or did not succeed."""

    OK = "ok"
    NOT_FOUND = "not_found"
    EXPIRED = "expired"
    ALREADY_USED = "already_used"


class ActionTokenRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, token: ActionToken) -> ActionToken:
        """Store a freshly minted token. Issuance lives elsewhere; this is the write seam."""
        self._session.add(token)
        await self._session.flush()
        return token

    async def consume_by_hash(
        self, token_hash: str, *, now: datetime | None = None
    ) -> tuple[ActionToken | None, ConsumeReason]:
        """Spend the token with this digest, once, and say what happened.

        The conditional ``UPDATE`` is the whole of the single-use guarantee: it matches only a
        row that is unexpired and unspent, and it sets the one column that makes the row spent
        in the same statement. If it matched nothing, the row is read to say *why* nothing
        matched. Committing on success means the spend is durable before the domain action that
        the token authorised begins.
        """
        moment = now or datetime.now(UTC)
        stmt = (
            update(ActionToken)
            .where(
                ActionToken.token_hash == token_hash,
                ActionToken.used_at.is_(None),
                ActionToken.expires_at > moment,
            )
            .values(used_at=moment)
            .returning(ActionToken.id)
        )
        result = await self._session.execute(stmt)
        token_id = result.scalar_one_or_none()
        if token_id is not None:
            await self._session.commit()
            return await self._session.get(ActionToken, token_id), ConsumeReason.OK

        existing = await self._session.execute(
            select(ActionToken).where(ActionToken.token_hash == token_hash)
        )
        token = existing.scalar_one_or_none()
        if token is None:
            return None, ConsumeReason.NOT_FOUND
        if token.used_at is not None:
            return None, ConsumeReason.ALREADY_USED
        return None, ConsumeReason.EXPIRED

    async def commit(self) -> None:
        await self._session.commit()

    async def find_by_id(self, token_id: uuid.UUID) -> ActionToken | None:
        return await self._session.get(ActionToken, token_id)
