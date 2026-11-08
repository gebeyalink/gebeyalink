"""FR-2301 — the client-support endpoint the app reads before it has a session.

The endpoint is deliberately public: a handset that has never signed in still has to learn
whether its build is accepted. These tests therefore call it with no Authorization header
and assert only the three contract fields, their Settings source, and the JSON envelope.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from pydantic import SecretStr

from apps.api.core.config import Settings
from apps.api.main import create_app

PATH = "/v1/meta/client-support"
CLIENT_VERSION_FIELDS = {
    "min_supported_app_version",
    "latest_app_version",
    "force_update_below",
}


def _settings(**overrides: object) -> Settings:
    """Configuration pointing nowhere, for a test that never starts a service."""
    values: dict[str, object] = {
        "database_url": "postgresql+psycopg://nobody@127.0.0.1:1/nothing",
        "redis_url": "redis://127.0.0.1:1/0",
        "signing_private_key": SecretStr("unused-outside-the-application"),
        "signing_public_key": SecretStr("unused-outside-the-application"),
        "gateway_secret": SecretStr("unused-outside-the-application"),
        "terms_version": "2026-01-01",
        "totp_encryption_key": SecretStr("ZGVmZ2hpamtsbW5vcHFyc3R1dnd4eXphYmNkZWZnaGk="),
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def _client(settings: Settings) -> TestClient:
    """The application with its Settings already resolved, and no lifespan services started.

    The endpoint reads Settings from the application state and nothing else, so entering the
    lifespan would only start a database engine and a Redis client this test never uses.
    """
    application = create_app(settings)
    application.state.settings = settings
    return TestClient(application)


def test_the_endpoint_is_public() -> None:
    """A caller with no session can ask whether its app version is supported."""
    response = _client(_settings()).get(PATH)

    assert response.status_code == 200
    assert "www-authenticate" not in response.headers


def test_the_response_has_exactly_the_three_contract_fields() -> None:
    """The app reads three named versions; an extra field would be a contract change."""
    body = _client(_settings()).get(PATH).json()

    assert set(body) == CLIENT_VERSION_FIELDS


def test_the_response_values_are_the_settings_values() -> None:
    """The endpoint reports deployment data, not a hard-coded compatibility policy."""
    settings = _settings(
        client_min_supported_version="1.4.2",
        client_latest_version="2.3.0",
        client_force_update_below="1.8.0",
    )

    body = _client(settings).get(PATH).json()

    assert body["min_supported_app_version"] == settings.client_min_supported_version
    assert body["latest_app_version"] == settings.client_latest_version
    assert body["force_update_below"] == settings.client_force_update_below


def test_the_response_is_json() -> None:
    """The contract says application/json, and the route says nothing else."""
    response = _client(_settings()).get(PATH)

    assert response.headers["content-type"] == "application/json"
