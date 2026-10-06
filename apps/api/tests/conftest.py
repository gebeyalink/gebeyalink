"""Test fixtures.

Tests run against a real PostgreSQL and a real Redis, both started fresh for this run and
torn down afterwards. Nothing here is stubbed: the account table is created by running the
actual migration, verification codes are written to and read back from a live Redis, and
sign-in goes in through the same application object that runs in production.

The one substitution is the messaging backend. No messages leave the machine, so the
backend records them instead — which is also how a test reads the code it needs to submit.
"""

from __future__ import annotations

import asyncio
import os
import re
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import psycopg
import psycopg.rows
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from redis import Redis as SyncRedis

from apps.api.core.config import Settings
from apps.api.main import create_app
from apps.api.providers.sms import RecordingSmsProvider
from packages.shared.money import FeePayer

REPO_ROOT = Path(__file__).resolve().parents[3]

CONTAINER_PREFIX = "gebeyalink-test"

TERMS_VERSION = "2026-01-01"

if sys.platform == "win32":  # pragma: no cover - platform specific
    # The database driver waits on ordinary sockets, and Windows' default loop cannot.
    # TestClient builds its loop from the policy, so setting it here covers every request
    # a test makes without any test needing to know about it.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def _free_port() -> int:
    """Ask the operating system for a port nothing is listening on."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=180, check=False
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args)} failed: {result.stderr.strip()}")
    return result


def _docker_available() -> bool:
    try:
        return _docker("info", "--format", "{{.ServerVersion}}").returncode == 0
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - environment dependent
        return False


def _remove_container(name: str) -> None:
    _docker("rm", "-f", name, check=False)


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    """A PostgreSQL instance that exists only for this test run."""
    if not _docker_available():  # pragma: no cover - environment dependent
        pytest.skip("Docker is not running, so the database tests cannot run")

    name = f"{CONTAINER_PREFIX}-postgres"
    port = _free_port()
    _remove_container(name)
    _docker(
        "run",
        "-d",
        "--rm",
        "--name",
        name,
        "-e",
        "POSTGRES_PASSWORD=gebeyalink-test",
        "-e",
        "POSTGRES_DB=gebeyalink",
        "-p",
        f"{port}:5432",
        # The PostGIS build, not the plain one: woredas hold a real geography column, and CI
        # runs this same image for integration tests.
        "postgis/postgis:16-3.4",
    )
    try:
        sync_url = f"postgresql://postgres:gebeyalink-test@127.0.0.1:{port}/gebeyalink"
        deadline = time.monotonic() + 90
        while True:
            # Readiness is probed from out here rather than from inside the container,
            # because a container can be ready while the published port is not, and the
            # first connection to arrive in that window is dropped rather than queued.
            try:
                with psycopg.connect(sync_url, connect_timeout=5):
                    break
            except psycopg.OperationalError:
                if time.monotonic() > deadline:  # pragma: no cover - environment dependent
                    pytest.fail("the test database did not become reachable")
                time.sleep(1)

        yield f"postgresql+psycopg://postgres:gebeyalink-test@127.0.0.1:{port}/gebeyalink"
    finally:
        _remove_container(name)


@pytest.fixture(scope="session")
def redis_url() -> Iterator[str]:
    """A Redis instance that exists only for this test run."""
    if not _docker_available():  # pragma: no cover - environment dependent
        pytest.skip("Docker is not running, so the store tests cannot run")

    name = f"{CONTAINER_PREFIX}-redis"
    port = _free_port()
    _remove_container(name)
    _docker("run", "-d", "--rm", "--name", name, "-p", f"{port}:6379", "redis:7-alpine")
    try:
        deadline = time.monotonic() + 60
        ready = False
        while time.monotonic() < deadline:
            probe = _docker("exec", name, "redis-cli", "ping", check=False)
            if probe.returncode == 0 and "PONG" in probe.stdout:
                ready = True
                break
            time.sleep(1)
        if not ready:  # pragma: no cover - environment dependent
            pytest.fail("the test store did not become ready")

        yield f"redis://127.0.0.1:{port}/0"
    finally:
        _remove_container(name)


@pytest.fixture(scope="session")
def signing_keys() -> tuple[str, str]:
    """A throwaway keypair for signing tokens.

    Generated per run and never written down: a test that reused a real deployment's key
    could not tell us anything, and a key checked into the repository would be a key
    somebody could use.
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private_pem, public_pem


@pytest.fixture(scope="session")
def inert_settings() -> Settings:
    """Configuration pointing nowhere, for tests that never start anything.

    Building an application now reads the configuration, because the route table depends on
    it, so a test that only wants to inspect routes still has to hand one over. The addresses
    here are deliberately addresses nothing is listening on: if such a test ever reached for
    the database or the store, it would fail rather than quietly talk to a real one.
    """
    return Settings(
        database_url="postgresql+psycopg://nobody@127.0.0.1:1/nothing",
        redis_url="redis://127.0.0.1:1/0",
        signing_private_key=SecretStr("unused-outside-the-application"),
        signing_public_key=SecretStr("unused-outside-the-application"),
        signing_key_id="test-key",
        terms_version=TERMS_VERSION,
    )


@pytest.fixture(scope="session")
def settings(postgres_url: str, redis_url: str, signing_keys: tuple[str, str]) -> Settings:
    """Configuration pointing at this run's throwaway services.

    Lifetimes are the production ones, so what the tests assert about a code's lifetime or
    a resend gap is the lifetime a real trader gets. The impatient variant below exists for
    the tests that have to sit through one.
    """
    private_pem, public_pem = signing_keys
    return Settings(
        database_url=postgres_url,
        redis_url=redis_url,
        signing_private_key=SecretStr(private_pem),
        signing_public_key=SecretStr(public_pem),
        signing_key_id="test-key",
        terms_version=TERMS_VERSION,
    )


@pytest.fixture(scope="session")
def impatient_settings(
    postgres_url: str, redis_url: str, signing_keys: tuple[str, str]
) -> Settings:
    """The same configuration with lifetimes shortened to what a test can sit through."""
    private_pem, public_pem = signing_keys
    return Settings(
        database_url=postgres_url,
        redis_url=redis_url,
        signing_private_key=SecretStr(private_pem),
        signing_public_key=SecretStr(public_pem),
        signing_key_id="test-key",
        terms_version=TERMS_VERSION,
        otp_ttl_seconds=2,
        otp_resend_cooldown_seconds=1,
    )


@pytest.fixture(scope="session")
def spray_settings(postgres_url: str, redis_url: str, signing_keys: tuple[str, str]) -> Settings:
    """Configuration for proving the daily per-address ceiling.

    The per-minute ceiling of ten would otherwise stop a single address long before it
    reaches twenty codes a day, and the test would be measuring the wrong guard. Raising
    only that ceiling leaves the daily one as the limit actually under test.
    """
    private_pem, public_pem = signing_keys
    return Settings(
        database_url=postgres_url,
        redis_url=redis_url,
        signing_private_key=SecretStr(private_pem),
        signing_public_key=SecretStr(public_pem),
        signing_key_id="test-key",
        terms_version=TERMS_VERSION,
        auth_endpoint_limit_per_minute_per_ip=100,
    )


@pytest.fixture()
def spray_client(
    spray_settings: Settings,
    clean_database: None,
    clean_store: None,
    sms_recorder: RecordingSmsProvider,
) -> Iterator[TestClient]:
    """The application with only the per-minute guard lifted."""
    application: FastAPI = create_app(spray_settings)
    with TestClient(application) as test_client:
        application.state.sms_provider = sms_recorder
        yield test_client


@pytest.fixture(scope="session", autouse=True)
def migrated_database(postgres_url: str) -> Iterator[None]:
    """Build the schema by running the real migration command.

    Running the migration rather than creating tables from the models is the point: it is
    the only way to find out that the migration and the models have drifted apart.
    """
    environment = {
        **os.environ,
        "DATABASE_URL": postgres_url,
        "REDIS_URL": "redis://127.0.0.1:1/0",
        "SIGNING_PRIVATE_KEY": "unused-outside-the-application",
        "SIGNING_PUBLIC_KEY": "unused-outside-the-application",
        "TERMS_VERSION": TERMS_VERSION,
    }
    result = subprocess.run(
        # Run through the interpreter that is running the tests, so the migration uses the
        # same Alembic install no matter how the suite was launched.
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if result.returncode != 0:  # pragma: no cover - environment dependent
        pytest.fail(f"migration failed:\n{result.stdout}\n{result.stderr}")
    yield


@pytest.fixture()
def clean_database(postgres_url: str) -> Iterator[None]:
    """Empty the tables each test writes to, so each starts from a known state.

    Accounts are emptied with a cascade, which takes their listings with them. The price
    index is emptied outright because it hangs off crops rather than accounts, and a row
    left behind by a price test would quietly judge the next test's listing.
    """
    sync_url = postgres_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(sync_url, autocommit=True) as connection:
        connection.execute("TRUNCATE TABLE users RESTART IDENTITY CASCADE")
        connection.execute("TRUNCATE TABLE price_indices")
    yield


@pytest.fixture()
def clean_store(redis_url: str) -> Iterator[None]:
    """Empty Redis so counters and sessions never leak between tests."""
    client = SyncRedis.from_url(redis_url)
    client.flushdb()
    yield
    client.flushdb()
    client.close()


@pytest.fixture()
def sms_recorder() -> Iterator[RecordingSmsProvider]:
    """The backend that captured the messages the trader would have received.

    Built here rather than read back off the application, so a test that runs the impatient
    configuration can still read the messages that configuration sent. Depending on one
    particular client would quietly point such tests at the wrong recorder.
    """
    recorder = RecordingSmsProvider()
    yield recorder


@pytest.fixture()
def client(
    settings: Settings, clean_database: None, clean_store: None, sms_recorder: RecordingSmsProvider
) -> Iterator[TestClient]:
    """The application, wired to this run's throwaway services."""
    application: FastAPI = create_app(settings)
    with TestClient(application) as test_client:
        # Installed after the lifespan has run: startup builds the configured backend, and
        # the recorder has to be the one in place by the time a request arrives.
        application.state.sms_provider = sms_recorder
        yield test_client


@pytest.fixture()
def impatient_client(
    impatient_settings: Settings,
    clean_database: None,
    clean_store: None,
    sms_recorder: RecordingSmsProvider,
) -> Iterator[TestClient]:
    """The application with shortened code lifetimes, for tests that wait one out."""
    application: FastAPI = create_app(impatient_settings)
    with TestClient(application) as test_client:
        application.state.sms_provider = sms_recorder
        yield test_client


@pytest.fixture()
def accounts(postgres_url: str) -> Callable[[], list[dict[str, object]]]:
    """Read the account rows straight out of PostgreSQL.

    Deliberately not through the application's own repository: a test that asks the code
    under test whether the code under test wrote a row proves very little.
    """
    sync_url = postgres_url.replace("postgresql+psycopg://", "postgresql://")

    def _accounts() -> list[dict[str, object]]:
        with (
            psycopg.connect(sync_url) as connection,
            connection.cursor(row_factory=psycopg.rows.dict_row) as cursor,
        ):
            cursor.execute(
                "SELECT phone_e164, role, language, kyc_tier, status, terms_version,"
                " terms_accepted_at FROM users ORDER BY phone_e164"
            )
            return list(cursor.fetchall())

    return _accounts


@pytest.fixture()
def signed_in(sign_in: Callable[..., httpx.Response]) -> Callable[..., dict[str, str]]:
    """Sign a trader in and hand back the headers their next request needs.

    Goes through the real sign-in journey rather than minting a token, so a test that calls a
    protected endpoint is exercising the same path a handset would take to get there.
    """

    def _signed_in(phone_e164: str = "+251911000001", **kwargs: object) -> dict[str, str]:
        verified = sign_in(phone_e164, **kwargs)
        assert verified.status_code == 200, verified.text
        return {"Authorization": f"Bearer {verified.json()['access_token']}"}

    return _signed_in


@pytest.fixture()
def profiles(postgres_url: str) -> Callable[[], list[dict[str, object]]]:
    """Read profile rows straight out of PostgreSQL.

    As with the account rows, going around the application's own repository is what makes
    this worth having: it shows what was actually stored, including whether a coordinate made
    it into the geography column at the precision it was supposed to.
    """
    sync_url = postgres_url.replace("postgresql+psycopg://", "postgresql://")

    def _profiles() -> list[dict[str, object]]:
        with (
            psycopg.connect(sync_url) as connection,
            connection.cursor(row_factory=psycopg.rows.dict_row) as cursor,
        ):
            cursor.execute(
                "SELECT display_name, region_code, woreda_id, kebele, business_name,"
                " license_no, tax_identification_number, version,"
                " ST_AsText(gps) AS gps_text, ST_SRID(gps) AS gps_srid,"
                " avatar_url"
                " FROM profiles ORDER BY user_id"
            )
            return list(cursor.fetchall())

    return _profiles


@pytest.fixture()
def listing_body() -> Callable[..., dict[str, object]]:
    """A valid listing body, with any field replaced.

    Written once here rather than in each test file so that a test which is about one rule
    does not have to restate the eleven that are not. Harvest and expiry are relative to
    today, because both are rules about the clock and a fixed date would make these tests
    fail on a different day than they were written.
    """

    def _listing_body(**overrides: object) -> dict[str, object]:
        now = datetime.now(UTC)
        body: dict[str, object] = {
            "crop_id": 1,
            "quantity_kg": 2_000,
            "min_order_kg": 100,
            "unit_price_santim": 3_250_000,
            "harvest_date": (now + timedelta(days=14)).date().isoformat(),
            "region_code": "OR",
            "woreda_id": 1,
            "delivery_options": ["PICKUP"],
            "delivery_fee_santim": 0,
            "description": "Red teff, well dried, from Adaa woreda",
            "organic": False,
        }
        body.update(overrides)
        return body

    return _listing_body


@pytest.fixture()
def stored_listings(postgres_url: str) -> Callable[[], list[dict[str, object]]]:
    """Read listing rows and their photographs straight out of PostgreSQL.

    As with the account and profile rows, a test that asks the service whether the service
    wrote a row proves very little. This is the database's own account of what was stored.
    """
    sync_url = postgres_url.replace("postgresql+psycopg://", "postgresql://")

    def _stored_listings() -> list[dict[str, object]]:
        with (
            psycopg.connect(sync_url) as connection,
            connection.cursor(row_factory=psycopg.rows.dict_row) as cursor,
        ):
            cursor.execute(
                "SELECT id, seller_id, source, crop_id, quantity_kg, sold_kg, min_order_kg,"
                " unit_price_santim, harvest_date, region_code, woreda_id, delivery_options,"
                " delivery_fee_santim, organic, status, price_outlier, short_code, version"
                " FROM listings ORDER BY created_at"
            )
            return list(cursor.fetchall())

    return _stored_listings


@pytest.fixture()
def stored_photos(postgres_url: str) -> Callable[[], list[dict[str, object]]]:
    """Read photograph rows straight out of PostgreSQL, in the order they claim."""
    sync_url = postgres_url.replace("postgresql+psycopg://", "postgresql://")

    def _stored_photos() -> list[dict[str, object]]:
        with (
            psycopg.connect(sync_url) as connection,
            connection.cursor(row_factory=psycopg.rows.dict_row) as cursor,
        ):
            cursor.execute(
                "SELECT listing_id, url, width, height, sort_order FROM listing_photos"
                " ORDER BY listing_id, sort_order"
            )
            return list(cursor.fetchall())

    return _stored_photos


@pytest.fixture()
def publish_price_index(postgres_url: str) -> Callable[..., None]:
    """Write a row of the price index, as the overnight aggregation would.

    Written directly because the aggregation is not part of this work: a price test needs an
    average to have been published before it can ask whether a listing was judged against it.
    """
    sync_url = postgres_url.replace("postgresql+psycopg://", "postgresql://")

    def _publish_price_index(
        *,
        crop_id: int = 1,
        region_code: str = "OR",
        average_santim: int | None = 3_250_000,
        sample_size: int = 40,
        insufficient_data: bool = False,
        day: date | None = None,
    ) -> None:
        with psycopg.connect(sync_url, autocommit=True) as connection:
            connection.execute(
                "INSERT INTO price_indices"
                " (crop_id, region_code, day, avg_unit_price_santim, sample_size,"
                "  insufficient_data)"
                " VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    crop_id,
                    region_code,
                    day or datetime.now(UTC).date(),
                    average_santim,
                    sample_size,
                    insufficient_data,
                ),
            )

    return _publish_price_index


@pytest.fixture()
def lapse_listing(postgres_url: str) -> Callable[[str], None]:
    """Move a listing's expiry into the past, behind the API's back.

    The alternative is waiting thirty days, and a test that cannot be run is not a test. This
    writes the one column the clock reads, which is exactly what time would have done to it.
    """
    sync_url = postgres_url.replace("postgresql+psycopg://", "postgresql://")

    def _lapse_listing(listing_id: str) -> None:
        with psycopg.connect(sync_url, autocommit=True) as connection:
            connection.execute(
                "UPDATE listings SET expires_at = now() - interval '1 second' WHERE id = %s",
                (listing_id,),
            )

    return _lapse_listing


@pytest.fixture()
def woredas_with_boundaries(postgres_url: str) -> Iterator[None]:
    """Three more woredas: one a short ride from the point traders give, one far off, one
    nobody has mapped.

    A radius is a statement about geography, and the pilot woreda on its own cannot show
    what one does: its boundary is deliberately empty, so every listing in it is unmeasured
    and a filter that quietly kept everything or quietly dropped everything would look
    exactly like one working. The three rows here make those two failures different. Two
    sit at known coordinates - one about a kilometre from the Addis point the tests give a
    trader, one about a hundred and eighty - and one carries no boundary at all, which is
    the state every woreda is in until the boundaries dataset lands.

    The rows come out again on the way out because nothing truncates this table: the seed
    integrity test counts the pilot woreda, so a leftover here would read as a change to the
    reference data rather than as one test's own footprint.
    """
    sync_url = postgres_url.replace("postgresql+psycopg://", "postgresql://")
    # Envelopes rather than hand-drawn rings: the shape only has to have a centroid where a
    # test can reason about it, and a box gives that centroid exactly.
    with psycopg.connect(sync_url, autocommit=True) as connection:
        connection.execute(
            "INSERT INTO woredas (id, name, region_code, zone, geog) VALUES"
            " (%s, %s, %s, %s, ST_Multi(ST_MakeEnvelope(%s, %s, %s, %s, 4326))::geography),"
            " (%s, %s, %s, %s, ST_Multi(ST_MakeEnvelope(%s, %s, %s, %s, 4326))::geography),"
            " (%s, %s, %s, %s, NULL)",
            (
                2,
                "Fentale",
                "OR",
                "East Shewa",
                38.74,
                9.02,
                38.76,
                9.04,
                3,
                "Dendi",
                "OR",
                "West Shewa",
                39.99,
                7.99,
                40.01,
                8.01,
                4,
                "Amuru",
                "OR",
                "West Shewa",
            ),
        )
    yield
    with psycopg.connect(sync_url, autocommit=True) as connection:
        for statement in (
            "DELETE FROM quantity_reservations WHERE listing_id IN"
            " (SELECT id FROM listings WHERE woreda_id IN (2, 3, 4))",
            "DELETE FROM orders WHERE listing_id IN"
            " (SELECT id FROM listings WHERE woreda_id IN (2, 3, 4))",
            "DELETE FROM offers WHERE listing_id IN"
            " (SELECT id FROM listings WHERE woreda_id IN (2, 3, 4))",
            "DELETE FROM listing_photos WHERE listing_id IN"
            " (SELECT id FROM listings WHERE woreda_id IN (2, 3, 4))",
            "DELETE FROM listings WHERE woreda_id IN (2, 3, 4)",
            "DELETE FROM woredas WHERE id IN (2, 3, 4)",
        ):
            connection.execute(statement)


@pytest.fixture()
def offer_body() -> Callable[..., dict[str, object]]:
    """A valid offer body, with any field replaced.

    Priced above the default listing's ask and below twice it, because an offer that only just
    met the ceiling would test the ceiling in every test that used it, and one that only just
    beat the ask would test the ask instead. A valid offer ought to be valid for reasons other
    than the boundary it is standing next to.
    """

    def _offer_body(**overrides: object) -> dict[str, object]:
        body: dict[str, object] = {
            "quantity_kg": 500,
            "price_santim": 3_300_000,
            "valid_hours": 72,
        }
        body.update(overrides)
        return body

    return _offer_body


@pytest.fixture()
def stored_offers(postgres_url: str) -> Callable[[], list[dict[str, object]]]:
    """Read offer rows straight out of PostgreSQL.

    The database's own account of what was stored, for the same reason the listing rows are
    read this way: a test that asks the API whether the API wrote a row proves very little.
    """
    sync_url = postgres_url.replace("postgresql+psycopg://", "postgresql://")

    def _stored_offers() -> list[dict[str, object]]:
        with (
            psycopg.connect(sync_url) as connection,
            connection.cursor(row_factory=psycopg.rows.dict_row) as cursor,
        ):
            cursor.execute(
                "SELECT id, listing_id, buyer_id, thread_id, thread_depth, quantity_kg,"
                " price_santim, valid_until, status, version"
                " FROM offers ORDER BY thread_depth, created_at"
            )
            return list(cursor.fetchall())

    return _stored_offers


@pytest.fixture()
def lapse_offer(postgres_url: str) -> Callable[[str], None]:
    """Move an offer's deadline into the past, behind the API's back.

    The alternative is asking for a window of zero hours, which the request rules refuse on
    purpose. Time is the only thing that should move a deadline forward, so this writes the one
    column the clock reads - exactly what waiting seven days would have done.
    """
    sync_url = postgres_url.replace("postgresql+psycopg://", "postgresql://")

    def _lapse_offer(offer_id: str) -> None:
        with psycopg.connect(sync_url, autocommit=True) as connection:
            connection.execute(
                "UPDATE offers SET valid_until = now() - interval '1 second' WHERE id = %s",
                (offer_id,),
            )

    return _lapse_offer


@pytest.fixture(scope="session")
def seller_pays_settings(
    postgres_url: str, redis_url: str, signing_keys: tuple[str, str]
) -> Settings:
    """The same configuration with the platform fee billed to the seller.

    AC-6.4 asks for the snapshot to be proven under both payers, and which side pays is
    configuration. A test that could only ever see the default would be testing one of the two
    policies the snapshot exists to distinguish.
    """
    private_pem, public_pem = signing_keys
    return Settings(
        database_url=postgres_url,
        redis_url=redis_url,
        signing_private_key=SecretStr(private_pem),
        signing_public_key=SecretStr(public_pem),
        signing_key_id="test-key",
        terms_version=TERMS_VERSION,
        fee_payer=FeePayer.SELLER,
    )


@pytest.fixture()
def seller_pays_client(
    seller_pays_settings: Settings,
    clean_database: None,
    clean_store: None,
    sms_recorder: RecordingSmsProvider,
) -> Iterator[TestClient]:
    """The application with the seller paying the platform fee."""
    application: FastAPI = create_app(seller_pays_settings)
    with TestClient(application) as test_client:
        application.state.sms_provider = sms_recorder
        yield test_client


@pytest.fixture()
def seller_pays_sign_in(
    seller_pays_client: TestClient, code_for: Callable[[str], str]
) -> Callable[..., httpx.Response]:
    """Sign a trader in against the seller-pays application.

    Written out again rather than shared with `sign_in`, and the reason matters: the engine and
    the Redis client are process-global singletons bound to the event loop their application's
    lifespan started, so a test may not drive two of these clients at once. Signing in through
    the default client and then making the request through this one would leave the second app
    querying a connection pool owned by the first app's loop, which fails in a way that looks
    nothing like the mistake it is.
    """

    def _seller_pays_sign_in(phone_e164: str = "+251911000001", **kwargs: object) -> httpx.Response:
        headers = (
            {"x-forwarded-for": kwargs.pop("from_address")} if "from_address" in kwargs else {}
        )
        requested = seller_pays_client.post(
            "/v1/auth/otp/request", json={"phone_e164": phone_e164}, headers=headers
        )
        assert requested.status_code == 200, requested.text
        body: dict[str, object] = {
            "phone_e164": phone_e164,
            "code": code_for(phone_e164),
            "terms_version": TERMS_VERSION,
        }
        verified: httpx.Response = seller_pays_client.post(
            "/v1/auth/otp/verify", json=body, headers=headers
        )
        return verified

    return _seller_pays_sign_in


@pytest.fixture()
def seller_pays_signed_in(
    seller_pays_sign_in: Callable[..., httpx.Response],
) -> Callable[..., dict[str, str]]:
    """Headers for a trader signed in against the seller-pays application."""

    def _seller_pays_signed_in(
        phone_e164: str = "+251911000001", **kwargs: object
    ) -> dict[str, str]:
        verified = seller_pays_sign_in(phone_e164, **kwargs)
        assert verified.status_code == 200, verified.text
        return {"Authorization": f"Bearer {verified.json()['access_token']}"}

    return _seller_pays_signed_in


@pytest.fixture(scope="session")
def cod_settings(postgres_url: str, redis_url: str, signing_keys: tuple[str, str]) -> Settings:
    """The same configuration with cash-on-delivery switched on.

    FR-2101's COD route is behind a flag, and a test that could only ever see the flag off would
    be testing the refusal rather than the flow. This is the configuration a deployment opens to
    run the pilot, which is exactly what the COD tests need to drive.
    """
    private_pem, public_pem = signing_keys
    return Settings(
        database_url=postgres_url,
        redis_url=redis_url,
        signing_private_key=SecretStr(private_pem),
        signing_public_key=SecretStr(public_pem),
        signing_key_id="test-key",
        terms_version=TERMS_VERSION,
        cod_enabled=True,
    )


@pytest.fixture()
def cod_client(
    cod_settings: Settings,
    clean_database: None,
    clean_store: None,
    sms_recorder: RecordingSmsProvider,
) -> Iterator[TestClient]:
    """The application with cash-on-delivery switched on."""
    application: FastAPI = create_app(cod_settings)
    with TestClient(application) as test_client:
        application.state.sms_provider = sms_recorder
        yield test_client


@pytest.fixture()
def cod_sign_in(
    cod_client: TestClient, code_for: Callable[[str], str]
) -> Callable[..., httpx.Response]:
    """Sign a trader in against the COD-enabled application.

    Written out again rather than shared with `sign_in`, and the reason matters: the engine and
    the Redis client are process-global singletons bound to the event loop their application's
    lifespan started, so a test may not drive two of these clients at once. Signing in through
    the default client and then making the request through this one would leave the second app
    querying a connection pool owned by the first app's loop, which fails in a way that looks
    nothing like the mistake it is.
    """

    def _cod_sign_in(phone_e164: str = "+251911000001", **kwargs: object) -> httpx.Response:
        headers = (
            {"x-forwarded-for": kwargs.pop("from_address")} if "from_address" in kwargs else {}
        )
        requested = cod_client.post(
            "/v1/auth/otp/request", json={"phone_e164": phone_e164}, headers=headers
        )
        assert requested.status_code == 200, requested.text
        body: dict[str, object] = {
            "phone_e164": phone_e164,
            "code": code_for(phone_e164),
            "terms_version": TERMS_VERSION,
        }
        verified: httpx.Response = cod_client.post(
            "/v1/auth/otp/verify", json=body, headers=headers
        )
        return verified

    return _cod_sign_in


@pytest.fixture()
def cod_signed_in(
    cod_sign_in: Callable[..., httpx.Response],
) -> Callable[..., dict[str, str]]:
    """Headers for a trader signed in against the COD-enabled application."""

    def _cod_signed_in(phone_e164: str = "+251911000001", **kwargs: object) -> dict[str, str]:
        verified = cod_sign_in(phone_e164, **kwargs)
        assert verified.status_code == 200, verified.text
        return {"Authorization": f"Bearer {verified.json()['access_token']}"}

    return _cod_signed_in


@pytest.fixture()
def cod_signed_in_as_buyer(
    cod_signed_in: Callable[..., dict[str, str]], postgres_url: str
) -> Callable[..., dict[str, str]]:
    """Sign a buyer in against the COD-enabled application.

    The role is set in the database directly, exactly as `signed_in_as_buyer` does: nothing in
    the API yet moves an account to buying, and these tests are about the COD order flow, not the
    trading-role journey.
    """
    sync_url = postgres_url.replace("postgresql+psycopg://", "postgresql://")

    def _cod_signed_in_as_buyer(phone_e164: str = "+251911000003") -> dict[str, str]:
        headers = cod_signed_in(phone_e164)
        with psycopg.connect(sync_url, autocommit=True) as connection:
            connection.execute(
                "UPDATE users SET role = 'BUYER' WHERE phone_e164 = %s", (phone_e164,)
            )
        return headers

    return _cod_signed_in_as_buyer


@pytest.fixture()
async def concurrent_client(
    settings: Settings, clean_database: None, clean_store: None, sms_recorder: RecordingSmsProvider
) -> AsyncIterator[httpx.AsyncClient]:
    """An HTTP client that can have two requests genuinely in flight at the same time.

    The synchronous client drives the application through one portal on one event loop, so two
    requests made from two threads still take their turns: they would never be inside the listing
    lock together, and a test built on that would prove nothing about row locking. This drives the
    ASGI application directly, so two coroutines really do overlap and really do hold two database
    connections while they do.

    The application's lifespan runs inside the test's own event loop, which is what keeps the
    engine and the store belonging to the loop that is using them.
    """
    application: FastAPI = create_app(settings)
    async with application.router.lifespan_context(application):
        application.state.sms_provider = sms_recorder
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://concurrent.test"
        ) as async_http:
            yield async_http


@pytest.fixture()
async def signed_in_concurrently(
    concurrent_client: httpx.AsyncClient, code_for: Callable[[str], str]
) -> Callable[..., Awaitable[dict[str, str]]]:
    """Sign a trader in over the concurrent client, so the tokens belong to the same run.

    Signed in through the synchronous client this would be a token from another process's event
    loop, which happens to work and is not a thing a test should rely on.
    """

    async def _sign_in(phone_e164: str = "+251911000001") -> dict[str, str]:
        requested = await concurrent_client.post(
            "/v1/auth/otp/request", json={"phone_e164": phone_e164}
        )
        assert requested.status_code == 200, requested.text
        verified = await concurrent_client.post(
            "/v1/auth/otp/verify",
            json={
                "phone_e164": phone_e164,
                "code": code_for(phone_e164),
                "terms_version": TERMS_VERSION,
            },
        )
        assert verified.status_code == 200, verified.text
        return {"Authorization": f"Bearer {verified.json()['access_token']}"}

    return _sign_in


@pytest.fixture()
def idempotency_key() -> Callable[[], str]:
    """A fresh idempotency key.

    A callable rather than a value because the whole point of the header is that two requests
    sharing one key are the same request: a test that wanted two separate orders has to be given
    two separate keys, and getting that wrong would show up as an order silently not being
    created rather than as a failure of the code under test.
    """
    return lambda: str(uuid.uuid4())


@pytest.fixture()
def order_body() -> Callable[..., dict[str, object]]:
    """A valid acceptance or buy-now body, with any field replaced.

    PICKUP and a free delivery fee, because a delivery method the listing has to offer and a fee
    somebody chose are two more rules for a test about a third one to trip over. The address is
    the pilot woreda's, because the column is a foreign key into the seeded reference data and an
    address nobody could have written is not a thing a handset can send.
    """

    def _order_body(**overrides: object) -> dict[str, object]:
        body: dict[str, object] = {
            "delivery_method": "PICKUP",
            "buyer_address_woreda_id": 1,
            "buyer_address_text": "Adaa, East Shewa",
        }
        body.update(overrides)
        return body

    return _order_body


@pytest.fixture()
def stored_orders(postgres_url: str) -> Callable[[], list[dict[str, object]]]:
    """Read order rows straight out of PostgreSQL.

    For the same reason the listing and offer rows are read this way: the database's own account
    of what was stored is evidence the service cannot fake, and the money frozen onto an order is
    the kind of thing worth reading back from something other than the code that wrote it.
    """
    sync_url = postgres_url.replace("postgresql+psycopg://", "postgresql://")

    def _stored_orders() -> list[dict[str, object]]:
        with (
            psycopg.connect(sync_url) as connection,
            connection.cursor(row_factory=psycopg.rows.dict_row) as cursor,
        ):
            cursor.execute(
                "SELECT id, short_code, buyer_id, seller_id, listing_id, offer_id, quantity_kg,"
                " unit_price_santim, delivery_method, delivery_fee_santim, platform_fee_santim,"
                " vat_santim, total_santim, fee_payer, buyer_address_woreda_id,"
                " buyer_address_text, pickup_code, status, cancel_reason, payment_mode,"
                " confirmed_at, buyer_confirmed_at, seller_confirmed_at, funded_at, ready_at,"
                " shipped_at, delivered_at, delivered_weight_kg, version, state_before_dispute"
                " FROM orders ORDER BY created_at, id"
            )
            return list(cursor.fetchall())

    return _stored_orders


@pytest.fixture()
def stored_reservations(postgres_url: str) -> Callable[[], list[dict[str, object]]]:
    """Read quantity reservation rows straight out of PostgreSQL.

    Availability is the sum of these and nothing else, so a test that asks the API how much of a
    listing is left is asking the thing under test to grade its own arithmetic. What is reserved,
    by what, and in what state, is read from the rows themselves.
    """
    sync_url = postgres_url.replace("postgresql+psycopg://", "postgresql://")

    def _stored_reservations() -> list[dict[str, object]]:
        with (
            psycopg.connect(sync_url) as connection,
            connection.cursor(row_factory=psycopg.rows.dict_row) as cursor,
        ):
            cursor.execute(
                "SELECT id, listing_id, holder_type, holder_id, quantity_kg, state, created_at,"
                " updated_at FROM quantity_reservations ORDER BY created_at, id"
            )
            return list(cursor.fetchall())

    return _stored_reservations


@pytest.fixture()
def lapse_order(postgres_url: str) -> Callable[[str], None]:
    """Move an order's funding window into the past, behind the API's back.

    The alternative is waiting forty-eight hours, and a test that cannot be run is not a test.
    This writes the one column the window is measured from, which is exactly what the clock would
    have done to it — and nothing else, so a test about expiry cannot pass by having quietly
    changed the order's status as well.
    """
    sync_url = postgres_url.replace("postgresql+psycopg://", "postgresql://")

    def _lapse_order(order_id: str) -> None:
        with psycopg.connect(sync_url, autocommit=True) as connection:
            connection.execute(
                "UPDATE orders SET created_at = now() - interval '49 hours' WHERE id = %s",
                (order_id,),
            )

    return _lapse_order


@pytest.fixture()
def backdate_confirmation(postgres_url: str) -> Callable[[str], None]:
    """Move an order's confirmation into the past, behind the API's back.

    The alternative is waiting seventy-two hours, and a test that cannot be run is not a test.
    This writes the column `seller_delayed` is measured from, which is exactly what the clock
    would have done to it — and nothing else, so a test about the delay cannot pass by having
    quietly changed the order's status as well.
    """
    sync_url = postgres_url.replace("postgresql+psycopg://", "postgresql://")

    def _backdate_confirmation(order_id: str) -> None:
        with psycopg.connect(sync_url, autocommit=True) as connection:
            connection.execute(
                "UPDATE orders SET confirmed_at = now() - interval '73 hours' WHERE id = %s",
                (order_id,),
            )

    return _backdate_confirmation


@pytest.fixture()
def backdate_ready(postgres_url: str) -> Callable[[str], None]:
    """Move a pickup order's readiness into the past, behind the API's back.

    The alternative is waiting twenty-four hours, and a test that cannot be run is not a test.
    This writes the one column the pickup window is measured from, which is exactly what the
    clock would have done to it — and nothing else, so a test about auto-confirmation cannot pass
    by having quietly changed the order's status as well.
    """
    sync_url = postgres_url.replace("postgresql+psycopg://", "postgresql://")

    def _backdate_ready(order_id: str) -> None:
        with psycopg.connect(sync_url, autocommit=True) as connection:
            connection.execute(
                "UPDATE orders SET ready_at = now() - interval '25 hours' WHERE id = %s",
                (order_id,),
            )

    return _backdate_ready


@pytest.fixture()
def backdate_shipped(postgres_url: str) -> Callable[[str], None]:
    """Move a shipment's departure into the past, behind the API's back.

    The alternative is waiting a crop's whole holding window, and a test that cannot be run is
    not a test. This writes the one column the shipment window is measured from, which is
    exactly what the clock would have done to it — and nothing else, so a test about automatic
    release cannot pass by having quietly changed the order's status as well.
    """
    sync_url = postgres_url.replace("postgresql+psycopg://", "postgresql://")

    def _backdate_shipped(order_id: str) -> None:
        with psycopg.connect(sync_url, autocommit=True) as connection:
            connection.execute(
                "UPDATE orders SET shipped_at = now() - interval " "'73 hours' WHERE id = %s",
                (order_id,),
            )

    return _backdate_shipped


@pytest.fixture()
def code_for(sms_recorder: RecordingSmsProvider) -> Callable[[str], str]:
    """Read the most recent code a trader was sent.

    Extracted from the recorded message rather than from the store, so the test proves the
    digits actually reached the handset and not merely that something was written down.
    """

    def _code_for(phone_e164: str) -> str:
        messages = sms_recorder.messages_to(phone_e164)
        assert messages, f"no message was recorded for {phone_e164}"
        found = re.search(r"\d{6}", messages[-1].body)
        assert found, f"the message carried no six-digit code: {messages[-1].body!r}"
        return found.group(0)

    return _code_for


@pytest.fixture()
def request_code(client: TestClient) -> Callable[[str], httpx.Response]:
    """Ask for a code the way a trader's handset does."""

    def _request_code(phone_e164: str) -> httpx.Response:
        response: httpx.Response = client.post(
            "/v1/auth/otp/request", json={"phone_e164": phone_e164}
        )
        return response

    return _request_code


@pytest.fixture()
def sign_in(client: TestClient, code_for: Callable[[str], str]) -> Callable[..., httpx.Response]:
    """Run the whole sign-in journey and hand back the verify response.

    `from_address` sets the forwarding header the platform's load balancer would set, and both
    requests carry it because the per-minute ceiling is applied to every route in the auth
    router. It exists for tests that need several traders: eleven buyers on one listing is a
    legitimate arrangement, but from a single address it would be one address signing in eleven
    times, which is exactly what the ceiling is for. Each trader signing in from their own
    address is the ordinary case, so the guard still applies to every one of them.
    """

    def _sign_in(
        phone_e164: str,
        *,
        terms_version: str = TERMS_VERSION,
        language: str | None = None,
        from_address: str | None = None,
    ) -> httpx.Response:
        headers = {"x-forwarded-for": from_address} if from_address is not None else {}
        requested = client.post(
            "/v1/auth/otp/request", json={"phone_e164": phone_e164}, headers=headers
        )
        assert requested.status_code == 200, requested.text
        body: dict[str, object] = {
            "phone_e164": phone_e164,
            "code": code_for(phone_e164),
            "terms_version": terms_version,
        }
        if language is not None:
            body["language"] = language
        verified: httpx.Response = client.post("/v1/auth/otp/verify", json=body, headers=headers)
        return verified

    return _sign_in


@pytest.fixture()
def signed_in_as_buyer(
    signed_in: Callable[..., dict[str, str]], postgres_url: str
) -> Callable[..., dict[str, str]]:
    """Sign a trader in as a buyer.

    Sign-in always produces a farmer, because that is the default every new account starts
    from, and nothing in the API yet moves an account to buying. The role is therefore set in
    the database directly: these tests are about what a buyer may put on a profile, and the
    journey for becoming one is not part of this task.
    """
    sync_url = postgres_url.replace("postgresql+psycopg://", "postgresql://")

    def _signed_in_as_buyer(phone_e164: str = "+251911000003") -> dict[str, str]:
        headers = signed_in(phone_e164)
        with psycopg.connect(sync_url, autocommit=True) as connection:
            connection.execute(
                "UPDATE users SET role = 'BUYER' WHERE phone_e164 = %s", (phone_e164,)
            )
        return headers

    return _signed_in_as_buyer
