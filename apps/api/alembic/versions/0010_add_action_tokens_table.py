"""the action tokens table

`action_tokens` is where FR-1102's second factor lives: a short, single-use, short-lived
token delivered in a notification SMS and required back before an offer may be accepted or a
delivery confirmed. §11.1 names the columns - ``user_id``, ``token_hash``, ``purpose``,
``expires_at``, ``used_at`` - and this migration adds them together with the row identity and
timestamps every table in the platform carries.

Three choices are worth stating rather than leaving to be inferred.

    ``token_hash`` is unique. The stored value is already a digest rather than the token, and
    uniqueness is what makes a lookup by token mean exactly one row; it also makes it
    impossible, at the database rather than in a handler, for one token to describe two
    actions.

    ``purpose`` is a single string carrying both the action and its target row
    (``offer_answer:<uuid>``, ``delivery_confirm:<uuid>``). §11.1 gives one column, and the
    target belongs with the action: a token minted for one offer must be useless for another.

    ``used_at`` is nullable and its nullness is the single-use marker. Consuming a token is a
    conditional update of this column, so two simultaneous confirmations of the same delivery
    cannot both see an unspent token.

``user_id`` references ``users.id`` with no cascade. Accounts are never hard-deleted, so
there is nothing for a cascade to do, and the plain foreign key is what records that.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "action_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("purpose", sa.String(length=80), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_action_tokens_user_id_users"),
        sa.PrimaryKeyConstraint("id", name="pk_action_tokens"),
        sa.UniqueConstraint("token_hash", name="uq_action_tokens_token_hash"),
    )


def downgrade() -> None:
    op.drop_table("action_tokens")
