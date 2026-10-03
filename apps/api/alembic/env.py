"""Migration environment.

The database address is read from the same environment the application reads, so a
migration can never be pointed somewhere the running service is not. Migrations also run
against an async driver, matching the application, so a column type that works in
production is the column type that was tested.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable, Coroutine
from logging.config import fileConfig
from typing import Any

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy.pool import NullPool

from apps.api.core.config import get_settings
from apps.api.db.base import Base
from apps.api.models import user as _user_model  # noqa: F401  (registers the table)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", get_settings().database_url)

#: Autogenerate compares the live database against this. Every mapped class must be
#: imported into the process for it to be seen.
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit the migration as SQL without connecting to anything.

    Used to review what a migration will do before letting it touch a real database.
    """
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
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

    Windows' default loop is built on a proactor that cannot wait on a plain socket, which
    is how a PostgreSQL driver connects. Every other platform's default loop is fine, so the
    substitution is confined to Windows and the loop is closed explicitly.
    """
    if sys.platform != "win32":
        asyncio.run(step())
        return
    loop = asyncio.SelectorEventLoop()
    try:
        loop.run_until_complete(step())
    finally:
        loop.close()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
