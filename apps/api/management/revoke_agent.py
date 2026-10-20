"""Take the agent role away, and end the relationships the agent holds.

    python -m apps.api.management.revoke_agent +251911000123

Revoking does two things and both matter. The ``user_roles`` row is marked revoked, so the
account stops being an agent to every route that checks the role. And every *active*
``agent_assignments`` row the agent holds is marked revoked, so the farmers they were carrying
stop being reachable through them at once — an agent who has been stood down must not still be
able to post a listing on a farmer's behalf.

Running the command again changes nothing and exits zero: the role row is already revoked and
the assignments already carry a revocation date, so a re-run is a no-op rather than a second
dated revocation. An account that never held the role is likewise a no-op, not an error.

Like approval, this is a pilot workaround for the admin console (FR-1301-1302), and the FR-1203
audit entry the console would write is deferred with §11.1's ``audit_log`` table.
"""

from __future__ import annotations

import sys

from apps.api.management._bootstrap import (
    build_role_admin,
    describe_failure,
    open_session,
    run,
)
from apps.api.models.user import AccountStatus


async def _revoke(phone_e164: str) -> int:
    async with open_session() as session:
        admin = build_role_admin(session)
        try:
            user, ended = await admin.revoke(phone_e164)
        except Exception as error:
            print(f"revoke_agent: refused: {describe_failure(error)}", file=sys.stderr)
            return 2
        if user.status in (AccountStatus.SUSPENDED, AccountStatus.CLOSED):
            # Revoking a suspended or closed account is still allowed — it may have been
            # suspended *after* being an agent — but it is worth saying so, since nothing
            # about the role was in force either way.
            print(
                f"revoke_agent: {phone_e164} revoked "
                f"({ended} assignment(s) ended); account is {user.status.value}",
            )
            return 0
        print(f"revoke_agent: {phone_e164} revoked ({ended} assignment(s) ended)")
        return 0


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1:
        print(
            "usage: python -m apps.api.management.revoke_agent <phone_e164>",
            file=sys.stderr,
        )
        return 64
    return run(lambda: _revoke(args[0]))


if __name__ == "__main__":
    raise SystemExit(main())
