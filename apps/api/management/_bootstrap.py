"""Shared plumbing for the operator commands.

Every command needs the same three things: the process configuration, one database session,
and the agent-role service over it. That is what this module holds, so the commands themselves
are only their own decision.

The session is built here rather than in each command for the reason the worker's runs give:
the engine is process-wide and the commands should not each invent their own. The one model
that must be imported for its registration side effect is ``users``: the tables a command
writes carry foreign keys to it, and SQLAlchemy resolves keys across the whole ``MetaData``
when it flushes, so a process that never loaded ``users`` fails at its first write.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.core.config import get_settings
from apps.api.db.session import dispose_engine, get_session_factory, init_engine

# Loaded for its registration side effect, which is what the noqa marks.
from apps.api.models import user as _user_model  # noqa: F401
from apps.api.repositories.agent_assignment_repository import AgentAssignmentRepository
from apps.api.repositories.user_repository import UserRepository
from apps.api.repositories.user_role_repository import UserRoleRepository
from apps.api.services.agent_service import AgentRoleAdmin
from packages.shared.errors import ApiError


@asynccontextmanager
async def open_session() -> AsyncIterator[AsyncSession]:
    """One session against the configured database, disposed when the command ends."""
    settings = get_settings()
    init_engine(settings)
    try:
        factory = get_session_factory()
        async with factory() as session:
            yield session
    finally:
        await dispose_engine()


def build_role_admin(session: AsyncSession) -> AgentRoleAdmin:
    """The approval/revocation service over one session."""
    return AgentRoleAdmin(
        users=UserRepository(session),
        user_roles=UserRoleRepository(session),
        assignments=AgentAssignmentRepository(session),
    )


def describe_failure(error: BaseException) -> str:
    """Turn a refusal into the one line an operator needs to read.

    A platform ``ApiError`` carries a code and the machine-readable reason behind it — the two
    things that say what to do next. Anything else is reported by its own message, so a bug is
    not silently relabelled as a policy refusal.
    """
    if isinstance(error, ApiError):
        detail = ", ".join(f"{key}={value}" for key, value in sorted(error.details.items()))
        return f"{error.code.value}: {detail}" if detail else error.code.value
    return f"{type(error).__name__}: {error}"


def run(step: Callable[[], Awaitable[int]]) -> int:
    """Drive one command body on an event loop the database driver can use.

    The loop is built here rather than left to the platform default, and for the same reason
    the migration environment builds its own: Windows' default loop is a proactor that cannot
    wait on a plain socket, which is how a PostgreSQL driver connects. A selector loop works on
    every platform this runs on, so it is the only one created — one path, closed explicitly,
    rather than two of which one is dead wherever the code is not running.
    """
    loop = asyncio.SelectorEventLoop()
    try:
        return loop.run_until_complete(step())
    finally:
        loop.close()
