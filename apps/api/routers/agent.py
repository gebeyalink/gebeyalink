"""Onboarding a farmer, from an agent's hand.

Two routes, and they are two because the transfer is two steps separated by a person reading
a message. The first asks the platform to prove the farmer's number; the second brings the
proof back. Neither lets the agent set the farmer's credentials, and neither lets the agent
choose a number the farmer did not prove — the farmer's own phone is the whole of the
authorization, which is FR-1202's point.

Both routes require the caller to hold the AGENT role. The role is checked here rather than
inside the service because it is a property of the caller, not of the request: a request
that arrives from a non-agent is refused before anything about the farmer is touched.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status

from apps.api.dependencies import AccessTokenDep, AgentServiceDep, DbSessionDep
from apps.api.models.user_role import Role
from apps.api.repositories.user_role_repository import UserRoleRepository
from apps.api.schemas.agent import (
    FarmerSummaryResponse,
    OnboardAccepted,
    OnboardConfirmBody,
    OnboardRequestBody,
)
from packages.shared.errors import ApiError, ErrorCode

router = APIRouter(prefix="/agent", tags=["Agents"])


async def require_agent_role(caller: AccessTokenDep, session: DbSessionDep) -> None:
    """Refuse the request unless the caller holds the AGENT role right now.

    Reads the role through its repository over this request's own session, so the check and
    the work that follows see the same state of the database.
    """
    if not await UserRoleRepository(session).has_active_role(caller.user_id, Role.AGENT):
        raise ApiError(ErrorCode.FORBIDDEN, details={"reason": "agent_role_required"})


RequireAgent = Depends(require_agent_role)


@router.post(
    "/farmers/onboard",
    response_model=OnboardAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[RequireAgent],
)
async def start_farmer_onboarding(
    body: OnboardRequestBody,
    caller: AccessTokenDep,
    agents: AgentServiceDep,
) -> OnboardAccepted:
    """Send a code to the farmer's phone and hand back a handle to the exchange.

    Nothing is created here. An account appears only when the farmer — not the agent — proves
    the number, which is what the second route does.
    """
    challenge = await agents.start_onboarding(caller.user_id, body.phone_e164)
    return OnboardAccepted(
        challenge_token=challenge.token,
        expires_in_seconds=challenge.expires_in_seconds,
    )


@router.post(
    "/farmers/onboard/confirm",
    response_model=FarmerSummaryResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[RequireAgent],
)
async def confirm_farmer_onboarding(
    body: OnboardConfirmBody,
    caller: AccessTokenDep,
    agents: AgentServiceDep,
) -> FarmerSummaryResponse:
    """Prove the farmer's number, create or recover the account, and record the relationship.

    The code is checked against the number the handle carries, never one the caller names, so
    the proof is bound to the exchange the agent started. On success the farmer exists, is
    linked to the agent for twelve months, and is returned — and nothing else about them is
    written by the agent's hand.
    """
    farmer = await agents.confirm_onboarding(caller.user_id, body.challenge_token, body.otp)
    return FarmerSummaryResponse(
        id=farmer.id,
        phone_e164=farmer.phone_e164,
        display_name=farmer.display_name,
    )
