"""Connection handling for the account database.

One engine per process, created lazily and closed on shutdown. Sessions are handed out per
unit of work and closed immediately afterwards, because a request that holds a connection
longer than it needs will starve the next one under load.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from apps.api.core.config import Settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def init_engine(settings: Settings) -> AsyncEngine:
    """Create this process's engine, once, and return it.

    Held process-wide rather than per request: a pool is expensive to build and is meant to
    outlive the calls that use it.
    """
    global _engine, _session_factory
    if _engine is None:
        _engine = create_async_engine(
            settings.database_url,
            pool_pre_ping=True,
            future=True,
        )
        _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """The session factory bound to this process's engine."""
    if _session_factory is None:
        raise RuntimeError("database engine has not been initialised")
    return _session_factory


async def dispose_engine() -> None:
    """Close every pooled connection, for a clean shutdown."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
