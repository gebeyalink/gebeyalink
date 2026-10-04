"""The liveness probe, held to what it promises.

`infra/Dockerfile.api` has asked for `/health` since the skeleton landed, and the security
scan in CI points at a running process, so the route is load-bearing rather than
incidental. These tests are unit-marked and need no database: the probe must answer even
when nothing else does.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.unit


def _bare_client() -> TestClient:
    """The app with no lifespan run, and so no services behind it.

    Used as a plain object rather than as a context manager on purpose: entering the
    context manager starts the application, which is what provisions the database engine
    and the store. The point of these tests is the state a healthcheck finds before
    anything has been provisioned.
    """
    from apps.api.main import create_app

    return TestClient(create_app())


def test_health_answers_without_a_sign_in() -> None:
    """A healthcheck carries no credentials, so the route may not require any."""
    response = _bare_client().get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_needs_no_database_or_store() -> None:
    """The probe runs before anything is provisioned, so it may not touch a service.

    This application has neither an engine nor a store client in its state, and no settings
    it could read a database address from. If the probe reached for one it would raise
    rather than answer, which is the failure this pins.
    """
    from apps.api.main import create_app

    application = create_app()
    response = TestClient(application).get("/health")

    assert response.status_code == 200
    assert "engine" not in application.state.__dict__


def test_health_carries_the_trace_header_every_response_carries() -> None:
    """One response shape, including on the one route that is not versioned."""
    response = _bare_client().get("/health")

    assert response.headers["X-Trace-Id"]


def test_health_sits_outside_the_versioned_prefix() -> None:
    """The Dockerfile addresses /health by a fixed path and must keep working."""
    assert _bare_client().get("/v1/health").status_code == 404
