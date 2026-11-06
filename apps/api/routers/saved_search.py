"""The caller's own saved searches.

Three routes and no path for somebody else's search: a saved search belongs to the token
that wrote it, so the account identity is never a thing the client states. GET lists, POST
saves, DELETE removes. The service owns the ten-per-user rule and the radius rule; this
module only turns the body into plain values and the row back into the published shape.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Response, status

from apps.api.dependencies import AccessTokenDep, SavedSearchServiceDep
from apps.api.models.notification import NotificationChannel
from apps.api.schemas.saved_search import SavedSearchBody, SavedSearchResponse
from apps.api.services.saved_search_service import SavedSearchDraft

router = APIRouter(prefix="", tags=["Search"])


@router.get("/me/searches", response_model=list[SavedSearchResponse])
async def read_my_searches(
    caller: AccessTokenDep,
    searches: SavedSearchServiceDep,
) -> list[SavedSearchResponse]:
    """Every saved search this account holds, newest first."""
    rows = await searches.list(caller.user_id)
    return [SavedSearchResponse.model_validate(row) for row in rows]


@router.post(
    "/me/searches",
    response_model=SavedSearchResponse,
    status_code=status.HTTP_201_CREATED,
)
async def save_search(
    body: SavedSearchBody,
    caller: AccessTokenDep,
    searches: SavedSearchServiceDep,
) -> SavedSearchResponse:
    """Save the search filter set under this account.

    The eleventh save is refused by the service with the contract's limit error; the route
    does not count anything itself, because a rule checked here would apply only to the HTTP
    door into a service the other channels are meant to share.
    """
    saved = await searches.create(
        caller.user_id,
        SavedSearchDraft(
            crop_id=body.crop_id,
            region_code=body.region_code,
            woreda_id=body.woreda_id,
            radius_km=body.radius_km,
            price_min=body.price_min,
            price_max=body.price_max,
            harvest_date_from=body.harvest_date_from,
            harvest_date_to=body.harvest_date_to,
            organic=body.organic,
            delivery_option=body.delivery_option,
            seller_tier_min=body.seller_tier_min,
            digest=body.digest,
            digest_channel=NotificationChannel(body.digest_channel.value),
        ),
    )
    return SavedSearchResponse.model_validate(saved)


@router.delete("/me/searches/{search_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_search(
    search_id: UUID,
    caller: AccessTokenDep,
    searches: SavedSearchServiceDep,
) -> Response:
    """Delete one saved search. A search that is already gone is still a success."""
    await searches.delete(caller.user_id, search_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
