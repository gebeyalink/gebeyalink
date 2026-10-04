"""The liveness probe.

One route, no dependencies, no database. It answers the only question a load balancer or
a container healthcheck is asking: is this process up and serving? A probe that reached
into the database would report a database outage as an API outage, and restarting the API
would not fix it — the restart would just move the failure somewhere harder to read.

Readiness is a different question, and nothing here answers it. When there is a readiness
check worth having, it gets its own route rather than being folded into this one.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="", tags=["Health"])


class HealthStatus(BaseModel):
    """What the probe reports.

    Deliberately plain: a body a client can parse without knowing anything about us.
    """

    status: str


@router.get("/health", response_model=HealthStatus)
async def health() -> HealthStatus:
    """Report that the process is serving. Always 200 while it is up."""
    return HealthStatus(status="ok")
