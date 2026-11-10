"""FR-1301: the moderator's listing search.

The moderation screen has to reach a listing in every state, not only what a buyer can see.
This file proves the search returns the right rows under each filter, that the cursor loses no
listing across pages, and that only a moderator or an admin may ask the question at all.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import cast

import httpx
import psycopg
import psycopg.rows
import pytest
from fastapi.testclient import TestClient

from apps.api.core import staff_totp

pytestmark = [pytest.mark.integration]

REPO_ROOT = Path(__file__).resolve().parents[3]
GATEWAY_SECRET = "test-gateway-secret"
TERMS_VERSION = "2026-01-01"
TOTP_ENCRYPTION_KEY = "ZGVmZ2hpamtsbW5vcHFyc3R1dnd4eXphYmNkZWZnaGk="

SUPPORT_EMAIL = "support-search@gebeyalink.example"
MODERATOR_EMAIL = "moderator-search@gebeyalink.example"
STAFF_PASSWORD = "a-good-long-staff-password"

SELLER_PHONE = "+251911210001"
SECOND_SELLER_PHONE = "+251911210002"

Rows = list[dict[str, object]]
Completed = subprocess.CompletedProcess[str]


# --------------------------------------------------------------------------- fixtures


@pytest.fixture()
def sql(postgres_url: str) -> Iterator[Callable[[str, tuple[object, ...] | None], Rows]]:
    """Run one statement straight against PostgreSQL and read any rows back."""
    sync_url = postgres_url.replace("postgresql+psycopg://", "postgresql://")

    def _run(statement: str, params: tuple[object, ...] | None = None) -> Rows:
        with (
            psycopg.connect(sync_url, autocommit=True) as connection,
            connection.cursor(row_factory=psycopg.rows.dict_row) as cursor,
        ):
            cursor.execute(statement, params)
            if cursor.description is None:
                return []
            return list(cursor.fetchall())

    yield _run


@pytest.fixture(autouse=True)
def clean_staff_tables(postgres_url: str) -> Iterator[None]:
    """Empty the staff tables before each test."""
    sync_url = postgres_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(sync_url, autocommit=True) as connection:
        connection.execute("TRUNCATE TABLE audit_log")
        connection.execute("TRUNCATE TABLE staff_sessions")
        connection.execute("TRUNCATE TABLE staff_users CASCADE")
    yield


@pytest.fixture()
def run_cli(postgres_url: str, redis_url: str) -> Callable[..., Completed]:
    """Run an operator command in its own process, as an operator would."""
    environment = {
        **os.environ,
        "DATABASE_URL": postgres_url,
        "REDIS_URL": redis_url,
        "SIGNING_PRIVATE_KEY": "unused-outside-the-application",
        "SIGNING_PUBLIC_KEY": "unused-outside-the-application",
        "GATEWAY_SECRET": GATEWAY_SECRET,
        "TERMS_VERSION": TERMS_VERSION,
        "TOTP_ENCRYPTION_KEY": TOTP_ENCRYPTION_KEY,
    }
    environment.pop("PYTHONIOENCODING", None)

    def _run(module: str, *args: str, stdin: str | None = None) -> Completed:
        return subprocess.run(
            [sys.executable, "-m", module, *args],
            cwd=REPO_ROOT,
            env=environment,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

    return _run


def _seed_staff(
    run_cli: Callable[..., Completed],
    *,
    email: str,
    role: str,
    password: str = STAFF_PASSWORD,
) -> Completed:
    """Seed a staff account through the CLI, feeding the password on standard input."""
    return run_cli(
        "apps.api.management.seed_staff",
        "--email",
        email,
        "--role",
        role,
        stdin=password + "\n",
    )


def _uri_from_stdout(stdout: str) -> str:
    return next(line for line in stdout.splitlines() if line.startswith("otpauth://"))


def _secret_from_uri(uri: str) -> bytes:
    import base64
    from urllib.parse import parse_qs, urlparse

    query = parse_qs(urlparse(uri).query)
    secret_b32 = query["secret"][0]
    padding = "=" * (-len(secret_b32) % 8)
    return base64.b32decode(secret_b32 + padding)


def _code(secret: bytes) -> str:
    return staff_totp.current_code(secret)


def _login(
    client: TestClient,
    email: str,
    secret: bytes,
    *,
    password: str = STAFF_PASSWORD,
) -> httpx.Response:
    return cast(
        httpx.Response,
        client.post(
            "/v1/admin/login",
            json={
                "email": email,
                "password": password,
                "totp_code": _code(secret),
            },
        ),
    )


def _full_token(client: TestClient, email: str, secret: bytes, password: str) -> str:
    """Sign in and change the password, returning a full-scoped token."""
    first = _login(client, email, secret, password=password)
    assert first.status_code == 200, first.text
    assert first.json()["scope"] == "change_password_only"
    changed = client.post(
        "/v1/admin/change-password",
        json={
            "old_password": password,
            "new_password": "the-new-long-staff-password",
            "totp_code": _code(secret),
        },
        headers={"Authorization": f"Bearer {first.json()['access_token']}"},
    )
    assert changed.status_code == 204, changed.text
    second = _login(client, email, secret, password="the-new-long-staff-password")
    assert second.status_code == 200, second.text
    assert second.json()["scope"] == "full"
    return cast(str, second.json()["access_token"])


def _staff_headers(
    client: TestClient,
    run_cli: Callable[..., Completed],
    *,
    email: str,
    role: str,
) -> dict[str, str]:
    seeded = _seed_staff(run_cli, email=email, role=role)
    assert seeded.returncode == 0, seeded.stdout + seeded.stderr
    secret = _secret_from_uri(_uri_from_stdout(seeded.stdout))
    token = _full_token(client, email, secret, STAFF_PASSWORD)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def support_headers(cod_client: TestClient, run_cli: Callable[..., Completed]) -> dict[str, str]:
    return _staff_headers(cod_client, run_cli, email=SUPPORT_EMAIL, role="SUPPORT")


@pytest.fixture()
def moderator_headers(cod_client: TestClient, run_cli: Callable[..., Completed]) -> dict[str, str]:
    return _staff_headers(cod_client, run_cli, email=MODERATOR_EMAIL, role="MODERATOR")


def _user_id(sql: Callable[..., Rows], phone_e164: str) -> str:
    rows = sql("SELECT id FROM users WHERE phone_e164 = %s", (phone_e164,))
    assert rows, phone_e164
    return str(rows[0]["id"])


def _create_listing(
    client: TestClient,
    headers: dict[str, str],
    listing_body: Callable[..., dict[str, object]],
    **overrides: object,
) -> str:
    response = client.post("/v1/listings", headers=headers, json=listing_body(**overrides))
    assert response.status_code == 201, response.text
    return cast(str, response.json()["id"])


def _page_ids(body: dict[str, object]) -> set[str]:
    data = cast(list[dict[str, object]], body["data"])
    return {cast(str, row["id"]) for row in data}


# ------------------------------------------------------------------- no-filter search


def test_no_filters_returns_every_listing_newest_first(
    cod_client: TestClient,
    moderator_headers: dict[str, str],
    cod_signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
) -> None:
    """The default page is everything, including rows a buyer search would hide."""
    seller = cod_signed_in(SELLER_PHONE)
    first = _create_listing(cod_client, seller, listing_body, description="first batch")
    second = _create_listing(cod_client, seller, listing_body, description="second batch")
    suspended = _create_listing(cod_client, seller, listing_body, description="third batch")
    assert (
        cod_client.post(
            f"/v1/admin/listings/{suspended}/suspend",
            json={"reason": "under review"},
            headers=moderator_headers,
        ).status_code
        == 200
    )

    response = cod_client.get("/v1/admin/listings", headers=moderator_headers)

    assert response.status_code == 200, response.text
    body = response.json()
    data = cast(list[dict[str, object]], body["data"])
    assert {row["id"] for row in data} == {first, second, suspended}
    assert {row["status"] for row in data} == {"ACTIVE", "SUSPENDED"}
    assert body["has_more"] is False
    assert body["next_cursor"] is None

    keys = [
        (
            datetime.fromisoformat(cast(str, row["created_at"])),
            uuid.UUID(cast(str, row["id"])),
        )
        for row in data
    ]
    assert keys == sorted(keys, reverse=True)

    assert set(data[0]) == {
        "id",
        "seller_id",
        "crop_id",
        "quantity_kg",
        "available_kg",
        "unit_price_santim",
        "status",
        "region_code",
        "short_code",
        "created_at",
    }


def test_status_filter_returns_only_suspended_listings(
    cod_client: TestClient,
    moderator_headers: dict[str, str],
    cod_signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
) -> None:
    seller = cod_signed_in(SELLER_PHONE)
    _create_listing(cod_client, seller, listing_body, description="still live")
    suspended = _create_listing(cod_client, seller, listing_body, description="taken down")
    assert (
        cod_client.post(
            f"/v1/admin/listings/{suspended}/suspend",
            json={"reason": "misleading photographs"},
            headers=moderator_headers,
        ).status_code
        == 200
    )

    response = cod_client.get(
        "/v1/admin/listings", params={"status": "SUSPENDED"}, headers=moderator_headers
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert _page_ids(body) == {suspended}
    assert [row["status"] for row in body["data"]] == ["SUSPENDED"]


def test_seller_filter_returns_only_that_sellers_listings(
    cod_client: TestClient,
    moderator_headers: dict[str, str],
    cod_signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
    sql: Callable[..., Rows],
) -> None:
    first_seller = cod_signed_in(SELLER_PHONE)
    second_seller = cod_signed_in(SECOND_SELLER_PHONE)
    first = _create_listing(cod_client, first_seller, listing_body, description="first seller one")
    second = _create_listing(cod_client, first_seller, listing_body, description="first seller two")
    _create_listing(cod_client, second_seller, listing_body, description="second seller")
    first_seller_id = _user_id(sql, SELLER_PHONE)

    response = cod_client.get(
        "/v1/admin/listings",
        params={"seller_id": first_seller_id},
        headers=moderator_headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert _page_ids(body) == {first, second}
    assert {row["seller_id"] for row in body["data"]} == {first_seller_id}


def test_description_search_is_case_insensitive_and_trimmed(
    cod_client: TestClient,
    moderator_headers: dict[str, str],
    cod_signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
) -> None:
    seller = cod_signed_in(SELLER_PHONE)
    teff = _create_listing(
        cod_client, seller, listing_body, description="RED TEFF, well dried, from Adaa"
    )
    _create_listing(cod_client, seller, listing_body, description="white wheat, clean")

    mixed_case = cod_client.get(
        "/v1/admin/listings", params={"q": "  red teff  "}, headers=moderator_headers
    )
    upper_case = cod_client.get(
        "/v1/admin/listings", params={"q": "WHITE WHEAT"}, headers=moderator_headers
    )

    assert mixed_case.status_code == 200, mixed_case.text
    assert _page_ids(mixed_case.json()) == {teff}
    assert upper_case.status_code == 200, upper_case.text
    assert len(upper_case.json()["data"]) == 1


def test_crop_region_and_status_filters_combine_with_and(
    cod_client: TestClient,
    moderator_headers: dict[str, str],
    cod_signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
) -> None:
    seller = cod_signed_in(SELLER_PHONE)
    wanted = _create_listing(
        cod_client,
        seller,
        listing_body,
        crop_id=2,
        region_code="SI",
        description="wanted combination",
    )
    _create_listing(
        cod_client,
        seller,
        listing_body,
        crop_id=2,
        region_code="SI",
        description="same crop and region but active",
    )
    _create_listing(
        cod_client,
        seller,
        listing_body,
        crop_id=1,
        region_code="SI",
        description="same region but different crop",
    )
    _create_listing(
        cod_client,
        seller,
        listing_body,
        crop_id=2,
        region_code="OR",
        description="same crop but different region",
    )
    assert (
        cod_client.post(
            f"/v1/admin/listings/{wanted}/suspend",
            json={"reason": "review"},
            headers=moderator_headers,
        ).status_code
        == 200
    )

    response = cod_client.get(
        "/v1/admin/listings",
        params={"crop_id": 2, "region_code": "SI", "status": "SUSPENDED"},
        headers=moderator_headers,
    )

    assert response.status_code == 200, response.text
    assert _page_ids(response.json()) == {wanted}


# ------------------------------------------------------------------------- permissions


def test_support_cannot_search_listings(
    cod_client: TestClient,
    support_headers: dict[str, str],
) -> None:
    """Support resolves a seller first; listing moderation is not a support tool."""
    response = cod_client.get("/v1/admin/listings", headers=support_headers)

    assert response.status_code == 403, response.text


def test_a_moderator_can_search_listings(
    cod_client: TestClient,
    moderator_headers: dict[str, str],
    cod_signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
) -> None:
    seller = cod_signed_in(SELLER_PHONE)
    listing = _create_listing(cod_client, seller, listing_body)

    response = cod_client.get("/v1/admin/listings", headers=moderator_headers)

    assert response.status_code == 200, response.text
    assert listing in _page_ids(response.json())


# --------------------------------------------------------------------------- pagination


def test_three_pages_of_ten_lose_no_listing_and_duplicate_none(
    cod_client: TestClient,
    moderator_headers: dict[str, str],
    cod_signed_in: Callable[..., dict[str, str]],
    listing_body: Callable[..., dict[str, object]],
) -> None:
    seller = cod_signed_in(SELLER_PHONE)
    created = {
        _create_listing(cod_client, seller, listing_body, description=f"batch {number}")
        for number in range(30)
    }

    seen: list[str] = []
    cursor: str | None = None
    for page in range(3):
        params: dict[str, object] = {"limit": 10}
        if cursor is not None:
            params["cursor"] = cursor
        response = cod_client.get("/v1/admin/listings", params=params, headers=moderator_headers)
        assert response.status_code == 200, response.text
        body = response.json()
        data = cast(list[dict[str, object]], body["data"])
        assert len(data) == 10
        assert body["has_more"] is (page < 2)
        seen.extend(cast(str, row["id"]) for row in data)
        cursor = cast(str | None, body["next_cursor"])
        if page < 2:
            assert cursor

    assert len(seen) == 30
    assert len(set(seen)) == 30
    assert set(seen) == created


# ------------------------------------------------------------------------ validation


def test_invalid_filters_are_refused(
    cod_client: TestClient,
    moderator_headers: dict[str, str],
) -> None:
    for params in (
        {"status": "NOT_A_STATUS"},
        {"seller_id": "not-a-uuid"},
        {"region_code": "XX"},
        {"q": "x" * 201},
        {"limit": 0},
    ):
        response = cod_client.get("/v1/admin/listings", params=params, headers=moderator_headers)
        assert response.status_code == 422, (params, response.text)
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"
