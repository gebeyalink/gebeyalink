"""Request and response bodies for the agent routes.

Three bodies, all small. The onboarding pair is a number in and a handle out, then a handle
and a code in and a farmer out. The farmer summary is deliberately the smallest thing that
identifies an account: the agent proved the number, and that is the whole of what the agent
is entitled to know about the farmer from this exchange.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from packages.shared.phone import ETHIOPIAN_MOBILE_PATTERN

PHONE_PATTERN = ETHIOPIAN_MOBILE_PATTERN.pattern


class OnboardRequestBody(BaseModel):
    """Ask to onboard a farmer: their number, entered by the agent."""

    model_config = ConfigDict(extra="forbid")

    phone_e164: str = Field(
        pattern=PHONE_PATTERN,
        description="The farmer's number in full international form.",
    )


class OnboardAccepted(BaseModel):
    """A code is on its way to the farmer, and a handle accompanies it."""

    model_config = ConfigDict(extra="forbid")

    challenge_token: str = Field(
        description="Opaque; present it back with the farmer's code to complete onboarding."
    )
    expires_in_seconds: int = Field(description="How long the code and the handle stay usable.")


class OnboardConfirmBody(BaseModel):
    """Complete the onboarding with the code the farmer read back."""

    model_config = ConfigDict(extra="forbid")

    challenge_token: str = Field(min_length=1, description="The handle from the request step.")
    otp: str = Field(pattern=r"^\d{6}$", description="The code the farmer received and read out.")


class FarmerSummaryResponse(BaseModel):
    """The farmer, as far as the agent who onboarded them may see."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    phone_e164: str
    display_name: str | None


class AgentRoleResponse(BaseModel):
    """The outcome of an approval or revocation, for the CLI to print."""

    model_config = ConfigDict(extra="forbid")

    user_id: UUID
    phone_e164: str
    role: str
    ended_assignments: int


class AgentAssignmentResponse(BaseModel):
    """One active relationship, as an agent or an admin may read it back."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    user_id: UUID
    agent_id: UUID
    expires_at: datetime
    created_at: datetime
