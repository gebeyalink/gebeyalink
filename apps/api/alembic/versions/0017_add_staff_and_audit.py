"""staff accounts, the sessions they sign in with, and the audit log

Three tables and one deferred foreign key, all of them §5.2 and §11.1's answer to the same
question: who may act on the platform's behalf, and what did they do.

``staff_users`` is a table apart from ``users`` on purpose. §5.2 is explicit that the staff
roles — ``VERIFIER``, ``SUPPORT``, ``MODERATOR``, ``ADMIN`` — are "separate staff accounts,
never shared with user accounts", so a staff member is not a trader with a bigger role; they
are a different kind of account with a different sign-in (a password and a one-time code, not
an OTP to a phone) and a different lifecycle. This table is that account.

``staff_sessions`` is the server's own record of a staff sign-in. A JWT alone cannot be
un-issued: a role change, a password change or a disablement must be able to end a live
session, and the only way to end a stateless token is to keep a row that says it is over. The
``expires_at`` here is written from ``admin_jwt_ttl_seconds`` at login and is the *authority*
on whether a session is live; the token's own ``exp`` is checked too, but a session row is what
the dependency trusts, so a clock-skewed verifier cannot extend a session past its row.

``audit_log`` is §11.1's append-only record. Its columns are the ones §11.1 names — ``actor_id``,
``actor_role``, ``action``, ``target_type``, ``target_id``, ``before_hash``, ``after_hash``,
``ip``, ``at`` — plus a surrogate ``id``. ``actor_id`` carries no foreign key because it may
name a staff account *or* a trader account (an agent onboarding a farmer acts as themselves),
and a column that points at one of two tables cannot point at either with a constraint.

The append-only enforcement is a trigger, and that is worth stating plainly.

    The application connects as the database's own superuser — the migration environment and
    the API read the same ``DATABASE_URL``, and in the pilot's deployments that role owns the
    schema. A ``REVOKE UPDATE, DELETE`` would therefore be inert: it would not constrain the
    role that actually writes. A ``BEFORE UPDATE OR DELETE`` trigger raises for every writer
    including the owner, and — critically — a trigger can only be removed by running DDL that
    drops it, which is a migration. That is the property an append-only log needs: the record
    cannot be rewritten by the same process that writes it, only by a deliberate migration that
    leaves a trail of its own.

``kyc_cases.reviewer_id`` finally gets its foreign key here. The 16-pre migration wrote the
column without one because no staff table existed to point at (§5.2's separate accounts); now
that it exists, the constraint is added. The column is nullable because a ``PENDING`` case has
no reviewer.

The monthly partitioning §11.1 describes for ``audit_log`` is deferred: the table is created
unpartitioned, and the decision to partition is recorded in the ADR rather than silently
omitted.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017"
down_revision: str = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: §5.2's staff roles. Created here, dropped only by this migration's downgrade.
STAFF_ROLE = postgresql.ENUM(
    "VERIFIER",
    "SUPPORT",
    "MODERATOR",
    "ADMIN",
    name="staff_role",
    create_type=False,
)

#: The trigger function and trigger that make ``audit_log`` append-only. The function raises on
#: any UPDATE or DELETE; the trigger fires it BEFORE the mutation, so the statement never lands.
#: ``RAISE EXCEPTION`` rather than a constraint, because an append-only rule is about the
#: statement, not about the row's values.
APPEND_ONLY_FUNCTION = """
CREATE OR REPLACE FUNCTION audit_log_reject_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only';
END;
$$ LANGUAGE plpgsql;
"""

APPEND_ONLY_TRIGGER = """
CREATE TRIGGER audit_log_append_only
    BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_reject_mutation();
"""


def upgrade() -> None:
    bind = op.get_bind()
    STAFF_ROLE.create(bind, checkfirst=True)

    op.create_table(
        "staff_users",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        # An email is a staff member's identifier before they have anything else, and it is
        # the thing an operator types into the seed command. 320 is the RFC 5321 ceiling.
        sa.Column("email", sa.String(length=320), nullable=False),
        # The scrypt string, parameters and salt included, so a parameter change re-hashes on
        # the next login rather than needing a schema change. See apps/api/core/staff_password.
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        # The TOTP secret, Fernet-encrypted. Text rather than bytea because it is a rendered
        # ciphertext (a Fernet token is base64), and the encrypted form is what is stored.
        sa.Column("totp_secret_encrypted", sa.Text(), nullable=False),
        sa.Column("role", STAFF_ROLE, nullable=False),
        # True until the staff member sets their own password. A freshly seeded or created
        # account can sign in only far enough to change it; no other admin route accepts it.
        sa.Column(
            "must_change_password",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        # Null while the account may sign in. Disabling is an UPDATE of this column, never a
        # delete: the staff member's history, and the audit rows naming them, stay readable.
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_staff_users")),
        sa.UniqueConstraint("email", name=op.f("uq_staff_users_email")),
    )

    op.create_table(
        "staff_sessions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("staff_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # Written at login from admin_jwt_ttl_seconds. The authority on whether a session is
        # live: the dependency reads it rather than trusting the token's exp alone.
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ip", postgresql.INET(), nullable=True),
        sa.ForeignKeyConstraint(
            ["staff_user_id"],
            ["staff_users.id"],
            name=op.f("fk_staff_sessions_staff_user_id_staff_users"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_staff_sessions")),
    )
    # The two reads the dependency and the revocation both make: "this staff member's live
    # sessions" (user_id where not revoked) and "the sessions that have lapsed".
    op.create_index(
        "ix_staff_sessions_staff_user_id_revoked_at",
        "staff_sessions",
        ["staff_user_id", "revoked_at"],
        unique=False,
    )
    op.create_index("ix_staff_sessions_expires_at", "staff_sessions", ["expires_at"], unique=False)

    op.create_table(
        "audit_log",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        # No foreign key: this may name a staff account or a trader account, so it can point
        # at neither with a constraint. Null for a system action with no human actor.
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=True),
        # A plain string, not an enum: the actor may be a staff role or a trader-side role
        # ('AGENT', 'SYSTEM'), and widening the vocabulary of an audit trail should not need a
        # type migration. 16 characters holds the longest of them.
        sa.Column("actor_role", sa.String(length=16), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("target_type", sa.String(length=32), nullable=False),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=True),
        # SHA-256 hex of the pre/post state, or null where a state is not a value that can be
        # serialized (a creation has no "before", a deletion has no "after").
        sa.Column("before_hash", sa.CHAR(length=64), nullable=True),
        sa.Column("after_hash", sa.CHAR(length=64), nullable=True),
        sa.Column("ip", postgresql.INET(), nullable=True),
        sa.Column(
            "at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
    )
    op.create_index("ix_audit_log_actor_id_at", "audit_log", ["actor_id", "at"], unique=False)
    op.create_index(
        "ix_audit_log_target_type_target_id",
        "audit_log",
        ["target_type", "target_id"],
        unique=False,
    )

    # Append-only, by trigger. See the module docstring for why a trigger rather than a grant.
    op.execute(APPEND_ONLY_FUNCTION)
    op.execute(APPEND_ONLY_TRIGGER)

    # 16-pre's deferred foreign key: the reviewer of a KYC case is a staff account.
    op.create_foreign_key(
        "fk_kyc_cases_reviewer",
        "kyc_cases",
        "staff_users",
        ["reviewer_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_kyc_cases_reviewer", "kyc_cases", type_="foreignkey")
    op.execute("DROP TRIGGER IF EXISTS audit_log_append_only ON audit_log")
    op.execute("DROP FUNCTION IF EXISTS audit_log_reject_mutation()")
    op.drop_index("ix_audit_log_target_type_target_id", table_name="audit_log")
    op.drop_index("ix_audit_log_actor_id_at", table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_index("ix_staff_sessions_expires_at", table_name="staff_sessions")
    op.drop_index("ix_staff_sessions_staff_user_id_revoked_at", table_name="staff_sessions")
    op.drop_table("staff_sessions")
    op.drop_table("staff_users")
    STAFF_ROLE.drop(op.get_bind(), checkfirst=True)
