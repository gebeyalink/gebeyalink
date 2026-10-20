"""Create the first staff account. Run from a terminal, once, at bootstrap.

    python -m apps.api.management.seed_staff --email ada@gebeyalink.example --role ADMIN

The chicken-and-egg this resolves: creating a staff account is an admin action, and there is no
admin until a staff account exists. Only the *first* admin needs this command; every subsequent
staff member is created through ``POST /v1/admin/staff``, which is itself audited.

The password is read from standard input and never from an argument. An argument is visible in
a shell's history and in the process list to every user on the machine, and a password leaked at
the moment it is set is the worst moment to leak it.

The provisioning URI is printed once, to standard output, and never stored or shown again. The
operator scans it into the new admin's authenticator; the secret it carries is encrypted at rest
and returned by no read route.

``--reset`` exists because an operator occasionally has to re-issue a credential to an account
that already exists — a lost phone, a forgotten password. It re-issues the TOTP secret and
resets the password, and it *refuses* without the flag rather than silently overwriting a live
account's credential.

The command writes an audit row with ``actor_role='SYSTEM'`` and ``action='STAFF_CREATED'``, so
the very first account is in the log that every later account's creation is in.
"""

from __future__ import annotations

import argparse
import getpass
import sys

from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.core import staff_password, staff_totp
from apps.api.core.config import get_settings
from apps.api.core.staff_secrets import StaffSecretCipher
from apps.api.management._bootstrap import describe_failure, open_session, run
from apps.api.models.staff_user import StaffRole, StaffUser
from apps.api.repositories.audit_log_repository import AuditLogRepository
from apps.api.repositories.staff_user_repository import StaffUserRepository
from apps.api.services.admin_service import ACTION_STAFF_CREATED, ACTOR_SYSTEM


def _read_password() -> str:
    """Read the new password without putting it on the command line.

    Interactive, this is ``getpass``: the prompt is shown, the typing is not echoed, and the
    secret never appears in the terminal's scrollback. Piped — which is how an operator's
    scripted provisioning and the test suite both drive this — ``getpass`` cannot read the
    terminal and would instead block or warn, so the password is taken as one line of standard
    input. Either way it never reaches ``argv``, where a shell history and the process list would
    hold it.
    """
    if sys.stdin.isatty():
        return getpass.getpass("New password: ")
    line = sys.stdin.readline()
    if not line:
        print("refused: no password on standard input", file=sys.stderr)
        return ""
    return line.rstrip("\r\n")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create or reset the first staff account.")
    parser.add_argument("--email", required=True, help="The staff member's email address.")
    parser.add_argument(
        "--role",
        required=True,
        choices=[member.value for member in StaffRole],
        help="The staff role.",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Re-issue credentials for an account that already exists.",
    )
    return parser.parse_args(argv)


async def seed(session: AsyncSession, args: argparse.Namespace) -> int:
    settings = get_settings()
    staff = StaffUserRepository(session)
    audit = AuditLogRepository(session)
    cipher = StaffSecretCipher(settings.totp_encryption_key.get_secret_value())
    email = args.email.lower()

    existing = await staff.find_by_email(email)
    if existing is not None and not args.reset:
        print(
            f"refused: {email} already exists; pass --reset to re-issue its credentials",
            file=sys.stderr,
        )
        return 2

    password = _read_password()
    if len(password) < 12:
        print("refused: the password must be at least 12 characters", file=sys.stderr)
        return 2

    secret = staff_totp.generate_secret()
    password_hash = staff_password.hash_password(
        password,
        n=settings.staff_password_scrypt_n,
        r=settings.staff_password_scrypt_r,
        p=settings.staff_password_scrypt_p,
    )

    if existing is None:
        row = await staff.add(
            StaffUser(
                email=email,
                password_hash=password_hash,
                totp_secret_encrypted=cipher.encrypt(secret),
                role=StaffRole(args.role),
                must_change_password=True,
            )
        )
    else:
        row = existing
        await staff.set_password(row, password_hash, must_change=True)
        await staff.set_totp_secret(row, cipher.encrypt(secret))
        await staff.set_role(row, StaffRole(args.role))

    await audit.record(
        actor_id=None,
        actor_role=ACTOR_SYSTEM,
        action=ACTION_STAFF_CREATED,
        target_type="staff_user",
        target_id=row.id,
        after={"email": row.email, "role": row.role.value},
    )
    await staff.commit()

    uri = staff_totp.provisioning_uri(row.email, secret)
    print(f"created {row.email} as {row.role.value}")
    print("provisioning URI (shown once; scan it into your authenticator):")
    print(uri)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    try:
        return run(lambda: _body(args))
    except Exception as error:
        print(describe_failure(error), file=sys.stderr)
        return 1


async def _body(args: argparse.Namespace) -> int:
    async with open_session() as session:
        return await seed(session, args)


if __name__ == "__main__":
    raise SystemExit(main())
