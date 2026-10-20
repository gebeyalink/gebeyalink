"""Migration environment.

The database address is read from the same environment the application reads, so a
migration can never be pointed somewhere the running service is not. Migrations also run
against an async driver, matching the application, so a column type that works in
production is the column type that was tested.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from logging.config import fileConfig
from typing import Any, Final

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy.pool import NullPool

from apps.api.core.config import get_settings
from apps.api.db.base import Base
from apps.api.models import action_token as _action_token_model  # noqa: F401  (registers it)
from apps.api.models import crop as _crop_model  # noqa: F401  (registers the table)
from apps.api.models import crop_unit as _crop_unit_model  # noqa: F401  (registers the table)
from apps.api.models import kyc_case as _kyc_case_model  # noqa: F401  (registers it)
from apps.api.models import listing as _listing_model  # noqa: F401  (registers the table)
from apps.api.models import notification as _notification_model  # noqa: F401  (registers it)
from apps.api.models import offer as _offer_model  # noqa: F401  (registers the table)
from apps.api.models import price_index as _price_index_model  # noqa: F401  (registers it)
from apps.api.models import profile as _profile_model  # noqa: F401  (registers the table)
from apps.api.models import region as _region_model  # noqa: F401  (registers the table)
from apps.api.models import user as _user_model  # noqa: F401  (registers the table)
from apps.api.models import woreda as _woreda_model  # noqa: F401  (registers the table)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

#: Whether this invocation will render SQL instead of applying it, read before anything
#: else. Offline runs are a review of what a migration would do, so they must not require a
#: database address or a signing key to be configured: a dry run is exactly the moment a
#: release engineer has no live environment to hand.
OFFLINE: bool = context.is_offline_mode()

if not OFFLINE:
    config.set_main_option("sqlalchemy.url", get_settings().database_url)

#: The database the offline renderer speaks. One database is deployed and one dialect is
#: rendered, so this names the real target rather than a guess.
_OFFLINE_DIALECT: Final[str] = "postgresql"

#: Autogenerate compares the live database against this. Every mapped class must be
#: imported into the process for it to be seen.
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit the migration as SQL without connecting to anything.

    Used to review what a migration will do before letting it touch a real database. The
    dialect is named rather than a URL read from the environment: generating SQL has to
    work with no live configuration to hand, and the dialect is the one fact about the
    target an offline run genuinely needs.
    """
    context.configure(
        dialect_name=_OFFLINE_DIALECT,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    """Run the migration against a real database."""
    _run(run_async_migrations)


def _run(step: Callable[[], Coroutine[Any, Any, None]]) -> None:
    """Drive a coroutine on an event loop the database driver can actually use.

    The loop is built here rather than left to the platform default. Windows' default loop
    is a proactor that cannot wait on a plain socket, which is exactly how a PostgreSQL
    driver connects, so on Windows the default is unusable. A selector loop works on every
    platform we deploy to, so it is the only one created — one path, closed explicitly,
    rather than two paths of which one is dead wherever the code is not running.
    """
    loop = asyncio.SelectorEventLoop()
    try:
        loop.run_until_complete(step())
    finally:
        loop.close()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
