"""The client-support metadata the app reads before it has a session.

The version values are deployment data, not policy: the compatibility window and the
deprecation notice are rules a deployment applies to those values, and this endpoint only
reports them. It is public because the app has no token yet at the moment it asks.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from apps.api.dependencies import settings_dep

router = APIRouter(prefix="/meta", tags=["Meta"])


class ClientSupportResponse(BaseModel):
    """The three versions the app checks at launch. No other field belongs here."""

    min_supported_app_version: str
    latest_app_version: str
    force_update_below: str


@router.get("/client-support", response_model=ClientSupportResponse)
async def client_support(settings: settings_dep) -> ClientSupportResponse:
    """Report the versions this deployment accepts and publishes.

    The endpoint does not enforce the values: it is the data feed the app and the release
    policy read. A deployment changes them through Settings without changing this route.
    """
    return ClientSupportResponse(
        min_supported_app_version=settings.client_min_supported_version,
        latest_app_version=settings.client_latest_version,
        force_update_below=settings.client_force_update_below,
    )
