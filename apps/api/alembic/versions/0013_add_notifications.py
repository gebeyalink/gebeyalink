"""the notifications table

``notifications`` is where a message owed to a trader waits its turn. A message used to exist
only as a string on its way out of a request; this table makes it a row, so the machinery that
hands it to a gateway can run on a clock rather than inside somebody's request, and so a
message that could not be delivered is still a message rather than a lost one. §11.1 names the
columns - ``user_id``, ``channel``, ``template_id``, ``language``, ``rendered_text``,
``status``, ``sent_at``, ``gateway_ref`` - and this migration adds them together with the row
identity and the two timestamps every table in the platform carries.

Three choices are worth stating rather than leaving to be inferred.

    ``priority`` is a small integer, not the lane's name. The lane a template declares is a
    build-time classification, and a queue is drained by an order the database can sort on:
    Q1 stores as 1, Q2 as 2 and Q3 as 3, so "highest lane first" is ``ORDER BY priority``
    rather than a translation the drain would have to do to its own rows.

    ``rendered_text`` is the text that will be sent, fixed at the moment the row is written.
    Rendering at drain time would ask the registry a question whose answer a deploy can
    change between the queue and the send, and it would make the row a template plus a
    context rather than a message. What is stored carries no phone number and no one-time
    code: a body in a table is a body everybody who can read the table can read.

    ``dedup_key`` is unique and nullable, and it is the row's identity in the event's terms
    rather than the database's. A producer that writes the same event twice is refused the
    second write with an ``IntegrityError`` instead of queueing a second message. Uniqueness
    is a property of the row, not of the send: a crash between the gateway accepting and the
    status being committed will send once more, and the drain is written to be at least
    once rather than exactly once.

Four columns are additive to §11.1 and are recorded under the pending contract amendments:

    ``failure_reason`` - why a row stopped. The vocabulary is the four short codes below;
    ``CAP_EXCEEDED`` and ``PROVIDER_REJECTED`` have no producer yet and are reserved for the
    slices that add the daily cap and the provider's own refusal.

    ``attempt_count`` - how many times the gateway has been asked, so the retry ladder knows
    which rung the next failure is on.

    ``next_attempt_at`` - when a row becomes eligible again, whether because a send failed or
    because the quiet window held it.

    ``dedup_key`` - described above.

``channel`` and ``status`` are enumerated in the database, created by this migration and
dropped only by the downgrade that created them, so a value a reader cannot interpret cannot
reach the table.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: str = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The channel a message goes out on. PUSH and INAPP are the vocabulary's other two members
#: and have no producer yet; SMS is the only one the platform can carry today.
NOTIFICATION_CHANNEL = postgresql.ENUM(
    "SMS",
    "PUSH",
    "INAPP",
    name="notification_channel",
    create_type=False,
)

#: Where a message sits in its own life. DELIVERED is the vocabulary's fourth member and is
#: reserved: this slice moves a row from QUEUED to SENT or to FAILED and no further.
NOTIFICATION_STATUS = postgresql.ENUM(
    "QUEUED",
    "SENT",
    "DELIVERED",
    "FAILED",
    name="notification_status",
    create_type=False,
)

#: The four short codes a row's ``failure_reason`` may hold. Two are produced today
#: (``RECIPIENT_UNAVAILABLE``, ``MAX_ATTEMPTS``) and two are reserved for later slices
#: (``CAP_EXCEEDED``, ``PROVIDER_REJECTED``); the constraint carries all four, because the
#: vocabulary is one list and the check is what keeps it one list.
FAILURE_REASON_CHECK = (
    "failure_reason IS NULL OR failure_reason IN "
    "('CAP_EXCEEDED', 'PROVIDER_REJECTED', 'MAX_ATTEMPTS', 'RECIPIENT_UNAVAILABLE')"
)


def upgrade() -> None:
    bind = op.get_bind()
    # Created explicitly, before the table that uses them, and dropped only by the downgrade
    # that created them: a type created here is this migration's to remove.
    NOTIFICATION_CHANNEL.create(bind, checkfirst=True)
    NOTIFICATION_STATUS.create(bind, checkfirst=True)

    op.create_table(
        "notifications",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("channel", NOTIFICATION_CHANNEL, nullable=False),
        sa.Column("template_id", sa.String(length=64), nullable=False),
        sa.Column("priority", sa.SmallInteger(), nullable=False),
        sa.Column("language", sa.String(length=2), nullable=False),
        sa.Column("rendered_text", sa.Text(), nullable=False),
        sa.Column(
            "status",
            NOTIFICATION_STATUS,
            server_default=NOTIFICATION_STATUS.enums[0],
            nullable=False,
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("gateway_ref", sa.String(length=64), nullable=True),
        # Additive to §11.1 - pending contract amendments. Null while a row has not failed.
        sa.Column("failure_reason", sa.String(length=32), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dedup_key", sa.String(length=128), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_notifications_user_id_users"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notifications")),
        sa.UniqueConstraint("dedup_key", name=op.f("uq_notifications_dedup_key")),
    )

    # The failure vocabulary is held as a check as well as the column's length, so a value
    # written by anything that reaches past the model is refused by the database rather than
    # stored for a reader to guess at.
    op.create_check_constraint(
        op.f("ck_notifications_failure_reason"),
        "notifications",
        sa.text(FAILURE_REASON_CHECK),
    )


def downgrade() -> None:
    op.drop_table("notifications")
    bind = op.get_bind()
    NOTIFICATION_STATUS.drop(bind, checkfirst=True)
    NOTIFICATION_CHANNEL.drop(bind, checkfirst=True)
