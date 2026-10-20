"""Approve an account as an agent. Run from a terminal, during the pilot.

    python -m apps.api.management.approve_agent +251911000123

The account must exist and be verified to ``TIER_1``; the identity behind that check is
established out of band, by the operator, before the command is run. What the command does is
write the one thing the API reads to let the account act as an agent: the ``user_roles`` row
with the ``AGENT`` role. Running it again on an account that already holds the role changes
nothing and exits zero, because a re-run is an operator's ordinary habit and not a failure.

This is a pilot workaround. The proper home for approval is the admin console (FR-1301-1302);
until that lands, an operator's shell is where the decision is recorded, and the FR-1203 audit
entry the console would write is deferred with the audit table itself (§11.1's ``audit_log``
is not in this schema yet).

Bootstrap-only; superseded by POST /v1/admin/agents/approve and /revoke, which audit identically.
Use the CLI only if staff auth is unavailable.

The command also checks that the account is not suspended or closed. An account in either of
those states is not one the platform will trade as, so it is not one that may act as an agent.
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


async def _approve(phone_e164: str) -> int:
    async with open_session() as session:
        admin = build_role_admin(session)
        try:
            user = await admin.approve(phone_e164)
        except Exception as error:
            print(f"approve_agent: refused: {describe_failure(error)}", file=sys.stderr)
            return 2
        if user.status in (AccountStatus.SUSPENDED, AccountStatus.CLOSED):
            print(f"approve_agent: refused: account is {user.status.value}", file=sys.stderr)
            return 2
        print(f"approve_agent: {phone_e164} now holds the AGENT role (user {user.id})")
        return 0


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1:
        print(
            "usage: python -m apps.api.management.approve_agent <phone_e164>",
            file=sys.stderr,
        )
        return 64
    return run(lambda: _approve(args[0]))


if __name__ == "__main__":
    raise SystemExit(main())
