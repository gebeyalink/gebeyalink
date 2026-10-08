"""The single-use tokens that make a sensitive SMS command friction against SIM-swap fraud.

An action token is the second factor on the two SMS commands that move things on a trader's
behalf — accepting an offer and confirming a delivery (FR-1102). The first factor is
possession of the phone number, which a swapped SIM defeats; the token is delivered in a
notification the real owner reads, is short enough to type back, and is spent the first time
it is used. An attacker who has taken the number still cannot act on a message the owner
already saw.

Two decisions are worth stating rather than leaving to be inferred.

Only the digest is stored. The token is a short string, and a table of short strings is a
table somebody can read; what is kept is the SHA-256 of the normalised token, so a copy of
this table is useless to whoever holds it. Normalisation and hashing live in
``packages.shared.messaging.action_tokens`` so the writer and the reader cannot disagree.

The purpose carries the target. ``offer_answer:<uuid>`` and ``delivery_confirm:<uuid>`` name
what the token unlocks and which row it unlocks, in one string rather than in a second
column. A token minted for one offer is therefore useless for another without any extra
lookup.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from apps.api.db.base import Base, TimestampMixin


class ActionToken(Base, TimestampMixin):
    """One token issued to one account for one purpose, spent at most once."""

    __tablename__ = "action_tokens"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Whose token it is. A plain foreign key rather than a cascade: an account is never hard
    # deleted, so a token is never orphaned, and the constraint is what says so.
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", name="fk_action_tokens_user_id_users"),
        nullable=False,
    )
    # SHA-256 hex of the normalised token. Unique, so the same token can never describe two
    # actions, and indexed by that uniqueness, so a lookup by token is one probe.
    token_hash: Mapped[str] = mapped_column(String(length=64), nullable=False, unique=True)
    # ``<prefix>:<uuid>`` where the prefix names the action and the uuid names the row. Kept
    # small enough to fit an index without being a text column.
    purpose: Mapped[str] = mapped_column(String(length=80), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Null until spent. Null is meaningful rather than missing: it is the single-use marker,
    # and the consume is a compare-and-set on it rather than a read followed by a write.
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
