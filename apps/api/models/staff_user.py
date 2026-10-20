"""A staff member: a separate account that acts on the platform's behalf.

§5.2 names four staff roles — ``VERIFIER``, ``SUPPORT``, ``MODERATOR``, ``ADMIN`` — and is
explicit that they are "separate staff accounts, never shared with user accounts". This table
is that separate account. It is not a trader with a bigger role: a staff member signs in with
an email, a password and a one-time code rather than an OTP to a phone, and the two lifecycles
are independent.

Nothing here is a secret kept in the clear. ``password_hash`` is a scrypt string with its own
parameters and salt inside it, and ``totp_secret_encrypted`` is a Fernet token; the plaintext
of neither ever reaches this table. A reader of this schema learns that a staff member exists
and what they may do, and nothing they could use to sign in as them.

``disabled_at`` is the account's off switch and is an UPDATE, never a delete. A disabled staff
member's history — the cases they reviewed, the audit rows naming them — has to stay readable,
so the row stays and the column says when it stopped being usable.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, Text, func, text
from sqlalchemy import Enum as SqlEnum
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from apps.api.db.base import Base
from apps.api.db.enums import stored_values


class StaffRole(enum.StrEnum):
    """§5.2's staff roles.

    ``ADMIN`` is the widest: it alone may create staff, disable staff and change roles.
    ``VERIFIER`` works the KYC queue. ``SUPPORT`` and ``MODERATOR`` are carried in the
    vocabulary because §5.2 names them; the routes that use them land in a later slice.
    """

    VERIFIER = "VERIFIER"
    SUPPORT = "SUPPORT"
    MODERATOR = "MODERATOR"
    ADMIN = "ADMIN"


class StaffUser(Base):
    """One staff account, and where it stands."""

    __tablename__ = "staff_users"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # The identifier an operator types into the seed command and the one a staff member signs
    # in with. Unique because two staff members with one address would be one ambiguous login.
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    # The scrypt string: algorithm, parameters, salt and digest together. Opaque here; the
    # one place that reads it is apps/api/core/staff_password.
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    # The TOTP secret, Fernet-encrypted. Text, because a Fernet token is a base64 string.
    totp_secret_encrypted: Mapped[str] = mapped_column(Text(), nullable=False)
    role: Mapped[StaffRole] = mapped_column(
        SqlEnum(StaffRole, name="staff_role", native_enum=True, values_callable=stored_values),
        nullable=False,
    )
    # True until the staff member sets their own password. A sign-in on an account in this
    # state yields a token that can do exactly one thing: change the password.
    must_change_password: Mapped[bool] = mapped_column(
        Boolean(), nullable=False, default=True, server_default=text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Null while the account may sign in; the moment it was disabled otherwise.
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def is_disabled(self) -> bool:
        return self.disabled_at is not None
