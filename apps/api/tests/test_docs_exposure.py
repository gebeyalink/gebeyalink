"""Whether the API serves a description of itself.

FastAPI mounts three routes that are not part of this API's design: an interactive browser at
`/docs`, a second at `/redoc`, and a machine-readable description of every route at
`/openapi.json`. It mounts them unconditionally unless told not to, and it is not the only
framework that would - a description of every endpoint, which one takes which secret, is a
map of the API handed to anyone who can reach the port.

So the flag is off unless something asks for it, and these tests hold both positions. They are
unit-marked and need no database: the question is which routes exist, and that is settled
while the application object is built rather than when it is started.

Nothing here contradicts the specification. `SPEC.md` §12.1 asks for the OpenAPI description
to live in the repository as `docs/openapi.yaml`, and it does; a served route is a separate
thing and is not required.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from apps.api.core.config import Settings
from apps.api.main import create_app

pytestmark = pytest.mark.unit

DOCS_ROUTES = ("/docs", "/redoc", "/openapi.json")


def _settings(**overrides: object) -> Settings:
    """Configuration pointing nowhere, for a test that never starts anything.

    Same shape as the shared `inert_settings` fixture, spelled out where it is needed so the
    one setting under test is the only thing this file has to vary.
    """
    values: dict[str, object] = {
        "database_url": "postgresql+psycopg://nobody@127.0.0.1:1/nothing",
        "redis_url": "redis://127.0.0.1:1/0",
        "signing_private_key": SecretStr("unused-outside-the-application"),
        "signing_public_key": SecretStr("unused-outside-the-application"),
        "terms_version": "2026-01-01",
    }
    return Settings(**values, **overrides)  # type: ignore[arg-type]


def _client(settings: Settings) -> TestClient:
    """A client for the application, with no lifespan run and so no services behind it.

    Plain object rather than a context manager: entering one would provision the database
    engine and the store, which is not what is being looked at here.
    """
    return TestClient(create_app(settings))


def _paths(application: FastAPI) -> set[str]:
    return {getattr(route, "path", "") for route in application.routes}


def test_the_description_is_not_served_by_default(inert_settings: Settings) -> None:
    """Nothing in the deployment asked for these routes, so they are not there."""
    assert inert_settings.expose_docs is False

    client = _client(inert_settings)

    for route in DOCS_ROUTES:
        response = client.get(route)
        assert response.status_code == 404, route


def test_the_routes_are_not_registered_either(inert_settings: Settings) -> None:
    """A route that exists and refuses is still a route.

    Checking the table is what distinguishes "not served" from "served by something that
    happens to answer 404": the first is a decision this code made, the second is a decision
    something else made later.
    """
    assert not _paths(create_app(inert_settings)) & set(DOCS_ROUTES)


def test_the_description_is_served_when_asked_for() -> None:
    """One flag brings back all three routes, and the description is a real OpenAPI document."""
    application = create_app(_settings(expose_docs=True))
    client = TestClient(application)

    assert set(DOCS_ROUTES) <= _paths(application)
    assert client.get("/docs").status_code == 200
    assert client.get("/redoc").status_code == 200

    description = client.get("/openapi.json")
    assert description.status_code == 200
    assert description.json()["openapi"].startswith("3.1")


def test_the_description_never_appears_in_a_response_body(inert_settings: Settings) -> None:
    """Turning the routes off must not leave the schema reachable by another name.

    A description served from anywhere else - a hand-written route, a debug dump - would give
    back everything turning these off was for.
    """
    client = _client(inert_settings)

    for route in ("/", "/openapi.yaml", "/v1/openapi.json", "/v1/docs"):
        assert client.get(route).status_code == 404, route
