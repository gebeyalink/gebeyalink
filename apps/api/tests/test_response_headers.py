"""Headers that every response carries, including the ones nobody planned.

`X-Content-Type-Options: nosniff` is set in the request middleware rather than on individual
handlers, so it holds for failures as well as successes. That distinction is the whole point:
a header added to the happy path leaves the error envelope unprotected, and the error envelope
is what a scanner and a caller both reach first when something is wrong.

These are unit-marked and need no database. A response is a response whether the process has
provisioned anything, and the 500 is provoked rather than waited for.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api.core.config import Settings
from apps.api.main import create_app

pytestmark = pytest.mark.unit


def _client(settings: Settings) -> TestClient:
    """A client with no lifespan run, and so no services behind it.

    Plain object rather than a context manager on purpose: entering one provisions the
    database engine and the store, and nothing here is about provisioned state.
    """
    return TestClient(create_app(settings))


def test_a_normal_response_declares_nosniff(inert_settings: Settings) -> None:
    response = _client(inert_settings).get("/health")

    assert response.status_code == 200
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_a_not_found_carries_it_too(inert_settings: Settings) -> None:
    """The error envelope is a response like any other, and clients parse it constantly."""
    response = _client(inert_settings).get("/v1/no-such-endpoint")

    assert response.status_code == 404
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_an_unexpected_failure_carries_it_too(inert_settings: Settings) -> None:
    """The 500 is the response least likely to be given the header by hand.

    Provoked by registering a route that raises rather than by breaking something real: the
    route is added to this application's own route table, the exception handler renders the
    500 envelope, and the header still has to be there. A 500 is also the response most
    likely to be assembled somewhere other than a handler that remembered.
    """
    application: FastAPI = create_app(inert_settings)

    @application.get("/v1/raise-for-test")
    def _raise() -> None:
        raise RuntimeError("provoked")

    # `raise_server_exceptions=False` because Starlette re-raises the exception after sending
    # the response, so the transport would hand the test the error rather than the 500 that
    # the client would have received.
    response = TestClient(application, raise_server_exceptions=False).get("/v1/raise-for-test")

    assert response.status_code == 500
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_nosniff_does_not_replace_the_trace_header(inert_settings: Settings) -> None:
    """Two policies on one response, and neither may quietly cost the other."""
    response = _client(inert_settings).get("/health")

    assert response.headers["X-Trace-Id"]
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_the_docs_routes_inherit_it_when_they_are_served() -> None:
    """A generated page is the response most likely to invite a browser to interpret it.

    Built with a settings object of its own rather than the shared fixture, because the flag
    has to be on for these routes to exist at all.
    """
    from pydantic import SecretStr

    enabled = TestClient(
        create_app(
            Settings(
                database_url="postgresql+psycopg://nobody@127.0.0.1:1/nothing",
                redis_url="redis://127.0.0.1:1/0",
                signing_private_key=SecretStr("unused-outside-the-application"),
                signing_public_key=SecretStr("unused-outside-the-application"),
                gateway_secret=SecretStr("unused-outside-the-application"),
                terms_version="2026-01-01",
                expose_docs=True,
            )
        )
    )

    for route in ("/docs", "/redoc", "/openapi.json"):
        response = enabled.get(route)
        assert response.status_code == 200, route
        assert response.headers["X-Content-Type-Options"] == "nosniff", route
