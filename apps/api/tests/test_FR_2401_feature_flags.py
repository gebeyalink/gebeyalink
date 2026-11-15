"""FR-2401: the flag table is the reader's only answer, and a missing row is a refusal.

The tests here are about the two questions a flag has to answer before any route can use it:
what did the migration install, and what does a read decide for one caller? The seed is read
straight from PostgreSQL, because the migration is what makes a fresh deployment's defaults a
fact. The decisions are read through the real repository and service inside an application's
lifespan, because the bucket arithmetic and the null rules are the service's job and a fake
session would only prove the fake.

Each read opens its own session. The tests arrange rows through a second connection, and a
long-lived session's identity map would otherwise hand back the before-arrangement row.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable, Iterator
from typing import cast

import httpx
import psycopg
import psycopg.rows
import pytest

from apps.api.db.session import get_session_factory
from apps.api.repositories.feature_flag_repository import FeatureFlagRepository
from apps.api.services.feature_flag_service import FeatureFlagService
from packages.shared.errors import ApiError, ErrorCode

pytestmark = [pytest.mark.integration]

Rows = list[dict[str, object]]

USER_ID = uuid.UUID("00000000-0000-4000-8000-000000000003")


def _sync_url(postgres_url: str) -> str:
    return postgres_url.replace("postgresql+psycopg://", "postgresql://")


@pytest.fixture()
def sql(postgres_url: str) -> Callable[[str, tuple[object, ...] | None], Rows]:
    """Run one statement and read any rows back, as the tests' arrangement channel."""
    sync_url = _sync_url(postgres_url)

    def _run(statement: str, params: tuple[object, ...] | None = None) -> Rows:
        with (
            psycopg.connect(sync_url, autocommit=True) as connection,
            connection.cursor(row_factory=psycopg.rows.dict_row) as cursor,
        ):
            cursor.execute(statement, params)
            if cursor.description is None:
                return []
            return list(cursor.fetchall())

    return _run


def _reset_flags(sql: Callable[..., Rows]) -> None:
    """Put the two seeded rows back and remove anything a test invented."""
    sql("DELETE FROM feature_flags WHERE name NOT IN ('cod_enabled', 'escrow_enabled')")
    sql(
        "INSERT INTO feature_flags (name, enabled, description) VALUES"
        " ('cod_enabled', false, 'Whether checkout may create cash-on-delivery orders.'),"
        " ('escrow_enabled', false, 'Whether buyers may fund an order the platform holds"
        " until delivery.')"
        " ON CONFLICT (name) DO UPDATE SET enabled = false, percentage_rollout = NULL,"
        " region_targeting = NULL, updated_at = now()"
    )


@pytest.fixture(autouse=True)
def clean_flags(sql: Callable[..., Rows]) -> Iterator[None]:
    """Every test starts and ends at the migration's seeded state."""
    _reset_flags(sql)
    yield
    _reset_flags(sql)


def _set_flag(
    sql: Callable[..., Rows],
    name: str,
    *,
    enabled: bool,
    percentage_rollout: int | None = None,
    region_targeting: list[str] | None = None,
) -> None:
    rows = sql(
        "UPDATE feature_flags SET enabled = %s, percentage_rollout = %s,"
        " region_targeting = %s WHERE name = %s RETURNING name",
        (enabled, percentage_rollout, region_targeting, name),
    )
    assert len(rows) == 1, name


async def _is_enabled(
    name: str,
    *,
    user_id: uuid.UUID | None = None,
    region_code: str | None = None,
) -> bool:
    """Read one flag through a fresh session, as a request would."""
    async with get_session_factory()() as session:
        service = FeatureFlagService(FeatureFlagRepository(session))
        return await service.is_enabled(name, user_id=user_id, region_code=region_code)


async def _list_flags() -> list[str]:
    async with get_session_factory()() as session:
        service = FeatureFlagService(FeatureFlagRepository(session))
        return [flag.name for flag in await service.list_flags()]


def test_the_migration_seeds_the_two_money_gates_off(sql: Callable[..., Rows]) -> None:
    """The only flags installed are the two this slice reads, and neither is on."""
    rows = sql(
        "SELECT name, enabled, percentage_rollout, region_targeting, description"
        " FROM feature_flags ORDER BY name"
    )

    assert [row["name"] for row in rows] == ["cod_enabled", "escrow_enabled"]
    assert [row["enabled"] for row in rows] == [False, False]
    assert [row["percentage_rollout"] for row in rows] == [None, None]
    assert [row["region_targeting"] for row in rows] == [None, None]
    assert all(cast(str, row["description"]) for row in rows)


async def test_the_editor_lists_the_installed_flags(
    concurrent_client: httpx.AsyncClient,
) -> None:
    """The reader returns rows in name order, so the editor's list is stable."""
    assert await _list_flags() == ["cod_enabled", "escrow_enabled"]


async def test_a_missing_flag_is_refused_rather_than_read_as_off(
    concurrent_client: httpx.AsyncClient,
    sql: Callable[..., Rows],
) -> None:
    """A name the deployment never installed is a deployment defect, not a false."""
    sql("DELETE FROM feature_flags WHERE name = 'cod_enabled'")

    with pytest.raises(ApiError) as raised:
        await _is_enabled("cod_enabled", user_id=USER_ID, region_code="OR")

    assert raised.value.code is ErrorCode.INTERNAL_ERROR
    assert raised.value.details == {"reason": "flag_missing", "flag": "cod_enabled"}


async def test_the_master_switch_wins_over_every_scope(
    concurrent_client: httpx.AsyncClient,
    sql: Callable[..., Rows],
) -> None:
    """A disabled flag is off even when its rollout and region would otherwise match."""
    _set_flag(
        sql,
        "escrow_enabled",
        enabled=False,
        percentage_rollout=100,
        region_targeting=["OR"],
    )

    assert await _is_enabled("escrow_enabled", user_id=USER_ID, region_code="OR") is False


async def test_the_percentage_rollout_is_a_stable_sha256_bucket(
    concurrent_client: httpx.AsyncClient,
    sql: Callable[..., Rows],
) -> None:
    """The bucket is the hex digest modulo one hundred, compared below the percentage."""
    _set_flag(sql, "escrow_enabled", enabled=True, percentage_rollout=50)
    bucket = int(hashlib.sha256(str(USER_ID).encode("utf-8")).hexdigest(), 16) % 100

    assert await _is_enabled("escrow_enabled", user_id=USER_ID) is (bucket < 50)

    _set_flag(sql, "escrow_enabled", enabled=True, percentage_rollout=100)
    assert await _is_enabled("escrow_enabled", user_id=USER_ID) is True

    _set_flag(sql, "escrow_enabled", enabled=True, percentage_rollout=0)
    assert await _is_enabled("escrow_enabled", user_id=USER_ID) is False


async def test_a_percentage_rollout_returns_before_region_targeting(
    concurrent_client: httpx.AsyncClient,
    sql: Callable[..., Rows],
) -> None:
    """Once a percentage is configured, the region list is not consulted below it."""
    _set_flag(
        sql,
        "escrow_enabled",
        enabled=True,
        percentage_rollout=100,
        region_targeting=["AA"],
    )
    assert await _is_enabled("escrow_enabled", user_id=USER_ID, region_code="OR") is True

    _set_flag(
        sql,
        "escrow_enabled",
        enabled=True,
        percentage_rollout=0,
        region_targeting=["OR"],
    )
    assert await _is_enabled("escrow_enabled", user_id=USER_ID, region_code="OR") is False


async def test_region_targeting_is_membership_and_a_missing_region_is_not_member(
    concurrent_client: httpx.AsyncClient,
    sql: Callable[..., Rows],
) -> None:
    """With no percentage, the region list decides, and no region belongs to none of it."""
    _set_flag(sql, "escrow_enabled", enabled=True, region_targeting=["OR", "AM"])

    assert await _is_enabled("escrow_enabled", region_code="OR") is True
    assert await _is_enabled("escrow_enabled", region_code="AA") is False
    assert await _is_enabled("escrow_enabled", region_code=None) is False


async def test_no_scopes_means_an_enabled_flag_is_on_for_everyone(
    concurrent_client: httpx.AsyncClient,
    sql: Callable[..., Rows],
) -> None:
    """Both optional columns null is the unscoped deployment, not an empty audience."""
    _set_flag(sql, "escrow_enabled", enabled=True)

    assert await _is_enabled("escrow_enabled", user_id=USER_ID, region_code="OR") is True
    assert await _is_enabled("escrow_enabled", user_id=None, region_code=None) is True
