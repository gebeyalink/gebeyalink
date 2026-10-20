"""The agent role, the farmers an agent carries, and the actions taken on their behalf.

An agent is the field mechanism for the pilot's exit criterion: a person who helps farmers
who cannot self-onboard. Three things are true of every action in this module, and they are
what the module exists to keep true.

An agent acts *for* a farmer only when there is an active, dated assignment saying so. The
relationship is not inferred from a shared device, a shared phone, or a role; it is a row
with an end date, and every write checks it first.

Onboarding is the farmer's own act, proved on the farmer's own phone. The agent enters the
farmer's number, an OTP goes to *that* number, and the farmer reads it back to the agent.
The agent never sets the farmer's credentials or chooses the farmer's number — the proof
that the number is theirs is the whole of FR-1202's ownership transfer.

The farmer's identity and money stay the farmer's. Nothing here can change a phone number,
a payout account or an OTP setting, and nothing here lets an agent accept an offer or confirm
a delivery as the farmer; those routes are owner-scoped and an agent's session is not the
farmer's. FR-1707 is most of the reason this module is shaped the way it is.

Every action here writes a row into §11.1's ``audit_log``, in the same transaction as the
action itself: the agent who onboarded a farmer and the agent who posted a listing on a
farmer's behalf are both named, when it happened, and what they touched. The four action
names are held as constants below so the writers and the readers share one vocabulary rather
than each deriving the strings from call sites.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from apps.api.core.config import Settings
from apps.api.models.user import AccountStatus, PrimaryRole, User, VerificationTier
from apps.api.models.user_role import Role
from apps.api.repositories.agent_assignment_repository import (
    AgentAssignmentRepository,
    assignment_expiry,
)
from apps.api.repositories.audit_log_repository import AuditLogRepository
from apps.api.repositories.profile_repository import ProfileRepository
from apps.api.repositories.user_repository import UserRepository
from apps.api.repositories.user_role_repository import UserRoleRepository
from apps.api.schemas.listing import ListingResponse
from apps.api.services.listing_service import ListingService
from apps.api.services.otp_service import OtpService
from apps.api.services.redis_challenge_store import AgentChallengeRepository
from packages.shared.errors import ApiError, ErrorCode
from packages.shared.i18n import Language
from packages.shared.phone import is_valid_ethiopian_mobile

#: What a newly onboarded farmer's primary label is. FR-1202 creates a farmer, and a farmer
#: is what a person who grows and sells is; the agent does not choose it and it is not
#: inferred from anything the agent sends.
ONBOARDED_PRIMARY_ROLE = PrimaryRole.FARMER

#: The farmer's reading language at onboarding. Amharic, because that is the platform default
#: and the agent channel's audience, and because the agent has no basis to choose a farmer's
#: language for them. The farmer may change it themselves later.
ONBOARDED_LANGUAGE = Language.AM

#: The action names §11.1's ``audit_log`` would carry, one per agent action this module
#: produces. Named here, once, so the admin slice that adds the table has the vocabulary
#: rather than re-deriving it from call sites.
ACTION_AGENT_APPROVED = "AGENT_APPROVED"
ACTION_AGENT_REVOKED = "AGENT_REVOKED"
ACTION_FARMER_ONBOARDED = "FARMER_ONBOARDED"
ACTION_LISTING_CREATED_ON_BEHALF = "LISTING_CREATED_ON_BEHALF"

#: The actor role carried when there is no human staff actor — a bootstrap command, or any
#: action the platform takes on its own. Held here so the CLI's audit rows and a system action's
#: rows agree on the word.
ACTOR_SYSTEM = "SYSTEM"

#: The actor role an agent's own action carries in the log. An agent acts as themselves — the
#: audit says an agent did it, which is different from the staff role an approval carries.
ACTOR_AGENT = "AGENT"


@dataclass(frozen=True, slots=True)
class OnboardChallenge:
    """The opaque handle an agent presents back with the farmer's code.

    The handle names the agent and the number — and only those. It carries no code and no
    account, because the account does not exist until the code is proved; what it lets the
    second request do is ask for the *same* (agent, phone) pair the first one asked about,
    so a challenge minted for one pair cannot be spent on another.
    """

    token: str
    expires_in_seconds: int


@dataclass(frozen=True, slots=True)
class FarmerSummary:
    """The farmer as the onboarding response describes them.

    Deliberately small: the identity of the account, the number that was proved, and the
    display name if one happens to exist. Nothing about money, tier or status is returned,
    because the agent is not the farmer and has no business reading it.
    """

    id: uuid.UUID
    phone_e164: str
    display_name: str | None


class AgentRoleAdmin:
    """Granting and revoking the agent role. The pilot's approval path.

    Kept apart from the rest of the agent service because it is a different kind of actor's
    work: this is the operator's decision, whether it arrives from a terminal (bootstrap) or
    from the admin route, and it needs four repositories and nothing else — no OTP provider,
    no listing path, no Redis. The service below is what an *agent* does once approved; this
    is what *makes* an agent and *unmakes* one, and the two are cleanly separable.

    Both methods are idempotent, because re-running a command or replaying a request is a
    normal thing, not an error: approving an account twice leaves one role, and revoking one
    that does not hold it changes nothing.

    Both write the audit row themselves, in the same transaction as the change. That is what
    makes the CLI and the route produce *identical* audit entries: they call these methods, and
    these methods are the only place AGENT_APPROVED and AGENT_REVOKED are written. There is no
    second site to drift.
    """

    def __init__(
        self,
        *,
        users: UserRepository,
        user_roles: UserRoleRepository,
        assignments: AgentAssignmentRepository,
        audit: AuditLogRepository,
    ) -> None:
        self._users = users
        self._user_roles = user_roles
        self._assignments = assignments
        self._audit = audit

    async def approve(self, phone_e164: str) -> User:
        """Grant an approved account the AGENT role, by phone. The CLI's entry point.

        Returns the account alone: the CLI only needs to report who was approved. The id-based
        entry point below returns the ``granted`` flag as well, because the route reports whether
        the approval was a fresh grant or an idempotent repeat.
        """
        user = await self._require_account_by_phone(phone_e164)
        approved, _granted = await self._approve(user)
        return approved

    async def approve_by_id(
        self,
        user_id: uuid.UUID,
        *,
        actor_id: uuid.UUID | None,
        actor_role: str,
        ip: str | None = None,
    ) -> tuple[User, bool]:
        """Grant the AGENT role to an account named by id. The admin route's entry point.

        ``granted`` is False when the account already held the role: the write is idempotent
        and the audit row is written either way, because a re-approval is still an administrative
        act and the log should say it happened.
        """
        user = await self._require_account_by_id(user_id)
        return await self._approve(user, actor_id=actor_id, actor_role=actor_role, ip=ip)

    async def revoke(self, phone_e164: str) -> tuple[User, int]:
        """Take the AGENT role away by phone, ending every relationship. The CLI's entry point."""
        user = await self._require_account_by_phone(phone_e164)
        return await self._revoke(user, actor_id=None, actor_role=ACTOR_SYSTEM, ip=None)

    async def revoke_by_id(
        self,
        user_id: uuid.UUID,
        *,
        actor_id: uuid.UUID | None,
        actor_role: str,
        ip: str | None = None,
    ) -> tuple[User, int]:
        """Take the AGENT role away by id, ending relationships. The admin route's entry point."""
        user = await self._require_account_by_id(user_id)
        return await self._revoke(user, actor_id=actor_id, actor_role=actor_role, ip=ip)

    async def _approve(
        self,
        user: User,
        *,
        actor_id: uuid.UUID | None = None,
        actor_role: str = ACTOR_SYSTEM,
        ip: str | None = None,
    ) -> tuple[User, bool]:
        """The one approval: gate on ``TIER_1``, grant once, audit once."""
        if user.kyc_tier != VerificationTier.TIER_1:
            raise ApiError(
                ErrorCode.FORBIDDEN,
                details={"reason": "tier_1_required", "kyc_tier": user.kyc_tier.value},
            )
        granted = await self._user_roles.grant(user.id, Role.AGENT)
        await self._audit.record(
            actor_id=actor_id,
            actor_role=actor_role,
            action=ACTION_AGENT_APPROVED,
            target_type="user",
            target_id=user.id,
            before={"agent": not granted},
            after={"agent": True},
            ip=ip,
        )
        await self._audit.commit()
        return user, granted

    async def _revoke(
        self,
        user: User,
        *,
        actor_id: uuid.UUID | None,
        actor_role: str,
        ip: str | None,
    ) -> tuple[User, int]:
        """The one revocation: role away, relationships ended, audited once."""
        revoked = await self._user_roles.revoke(user.id, Role.AGENT)
        ended = await self._assignments.revoke(agent_id=user.id)
        await self._audit.record(
            actor_id=actor_id,
            actor_role=actor_role,
            action=ACTION_AGENT_REVOKED,
            target_type="user",
            target_id=user.id,
            before={"agent": revoked},
            after={"agent": False, "assignments_ended": ended},
            ip=ip,
        )
        await self._audit.commit()
        return user, ended

    async def _require_account_by_phone(self, phone_e164: str) -> User:
        user = await self._users.find_by_phone(phone_e164)
        if user is None:
            raise ApiError(ErrorCode.NOT_FOUND, details={"reason": "account_not_found"})
        return user

    async def _require_account_by_id(self, user_id: uuid.UUID) -> User:
        user = await self._users.find_by_id(user_id)
        if user is None:
            raise ApiError(ErrorCode.NOT_FOUND, details={"reason": "account_not_found"})
        return user


class AgentService:
    """The farmers an agent carries and the actions taken on their behalf."""

    def __init__(
        self,
        *,
        users: UserRepository,
        assignments: AgentAssignmentRepository,
        challenges: AgentChallengeRepository,
        profiles: ProfileRepository,
        otp: OtpService,
        listings: ListingService,
        audit: AuditLogRepository,
        settings: Settings,
    ) -> None:
        self._users = users
        self._assignments = assignments
        self._challenges = challenges
        self._profiles = profiles
        self._otp = otp
        self._listings = listings
        self._audit = audit
        self._settings = settings

    # -- onboarding ------------------------------------------------------------

    async def start_onboarding(self, agent_id: uuid.UUID, phone_e164: str) -> OnboardChallenge:
        """Begin FR-1202's transfer: send a code to the farmer's own phone.

        The agent has already been established as an agent by the route. What this does not
        do is any part of creating an account yet: the code is the proof that the number
        belongs to the person standing in front of the agent, and until it comes back
        correctly there is no farmer to create. The challenge names the pair and nothing
        else, and it expires with the code.
        """
        if not is_valid_ethiopian_mobile(phone_e164):
            raise ApiError(
                ErrorCode.VALIDATION_ERROR,
                details={"fields": {"phone_e164": "not_a_valid_ethiopian_mobile_number"}},
            )
        existing = await self._users.find_by_phone(phone_e164)
        if existing is not None and existing.status in (
            AccountStatus.SUSPENDED,
            AccountStatus.CLOSED,
        ):
            # A closed or suspended account is not a farmer anyone may take on. Refused here
            # rather than at confirmation, so no code is spent proving a number that will
            # never be onboarded.
            raise ApiError(
                ErrorCode.FORBIDDEN,
                details={
                    "reason": "account_not_onboardable",
                    "account_status": existing.status.value,
                },
            )

        dispatch = await self._otp.send_code(phone_e164, ONBOARDED_LANGUAGE)
        token = await self._challenges.mint(
            agent_id=agent_id,
            phone_e164=phone_e164,
            ttl_seconds=dispatch.expires_in_seconds,
        )
        return OnboardChallenge(token=token, expires_in_seconds=dispatch.expires_in_seconds)

    async def confirm_onboarding(
        self,
        agent_id: uuid.UUID,
        challenge_token: str,
        otp: str,
        *,
        now: datetime | None = None,
    ) -> FarmerSummary:
        """Prove the farmer's number and take them on.

        The challenge is read first and the code checked second, in that order, so a wrong
        or expired handle never reaches the OTP store — and the code is checked against the
        *challenged* number, not one the caller supplies, so a challenge for one farmer
        cannot be spent on another.

        Then the account exists (or is recovered if the farmer already had one) and the
        relationship is written in the same transaction. If the agent already carries this
        farmer, the one active row is refreshed rather than duplicated — a re-confirmation
        is the farmer's own act and FR-1707 says that is what renews the relationship.
        """
        moment = now or datetime.now(UTC)
        challenge = await self._challenges.consume(challenge_token, agent_id=agent_id)
        phone_e164 = challenge.phone_e164

        await self._otp.check_code(phone_e164, otp)

        farmer = await self._users.find_by_phone(phone_e164)
        if farmer is None:
            farmer = await self._users.add(
                User(
                    id=uuid.uuid4(),
                    phone_e164=phone_e164,
                    role=ONBOARDED_PRIMARY_ROLE,
                    language=ONBOARDED_LANGUAGE,
                    status=AccountStatus.ACTIVE,
                    # The version of the terms in force at the moment the agent onboarded
                    # them, recorded from the agent's action. The *acceptance* is the
                    # farmer's own and is not stamped here: it is recorded when the farmer
                    # signs in and reads the terms for themselves.
                    terms_version=self._settings.terms_version,
                    terms_accepted_at=None,
                )
            )

        existing = await self._assignments.find_active_pair(
            user_id=farmer.id, agent_id=agent_id, now=moment
        )
        if existing is not None:
            await self._assignments.refresh(existing, now=moment)
        else:
            await self._assignments.create(
                user_id=farmer.id,
                agent_id=agent_id,
                expires_at=assignment_expiry(moment),
            )
        await self._assignments.commit()

        # FR-1203's record, in the transaction that created the relationship. The actor is the
        # agent — the person who did it — and the target is the farmer whose number was proved.
        await self._audit.record(
            actor_id=agent_id,
            actor_role=ACTOR_AGENT,
            action=ACTION_FARMER_ONBOARDED,
            target_type="user",
            target_id=farmer.id,
            after={"phone_e164": phone_e164, "agent_id": str(agent_id)},
        )
        await self._audit.commit()

        return FarmerSummary(
            id=farmer.id,
            phone_e164=phone_e164,
            display_name=await self._display_name(farmer.id),
        )

    # -- delegated listing -----------------------------------------------------

    async def create_listing_on_behalf(
        self,
        agent_id: uuid.UUID,
        farmer_id: uuid.UUID,
        body: dict[str, Any],
    ) -> ListingResponse:
        """Post a listing as the farmer, because an active assignment says the agent may.

        The gate is one row: an active, unrevoked, unexpired assignment between this agent
        and this farmer. Without it the request is refused with 403 and nothing is written.
        With it, the listing's ``seller_id`` is the *farmer* — not the agent — and the
        ``on_behalf_of_user_id`` names the farmer whose behalf it was posted on, so the row
        reads the way it should: the farmer's listing, carried by an agent.

        The listing goes through the same creation path every other door uses, so every rule
        about a listing applies here too and there is no second implementation to drift.
        """
        assignment = await self._assignments.find_active_pair(user_id=farmer_id, agent_id=agent_id)
        if assignment is None:
            raise ApiError(
                ErrorCode.FORBIDDEN,
                details={"reason": "no_active_assignment", "user_id": str(farmer_id)},
            )
        listing = await self._listings.create_on_behalf(
            agent_id=agent_id,
            farmer_id=farmer_id,
            body=body,
        )
        # FR-1203's record for the delegated act: the agent posted it, on the farmer's behalf.
        await self._audit.record(
            actor_id=agent_id,
            actor_role=ACTOR_AGENT,
            action=ACTION_LISTING_CREATED_ON_BEHALF,
            target_type="listing",
            target_id=listing.id,
            after={"farmer_id": str(farmer_id), "agent_id": str(agent_id)},
        )
        await self._audit.commit()
        return listing

    # -- helpers ---------------------------------------------------------------

    async def _display_name(self, user_id: uuid.UUID) -> str | None:
        """The farmer's display name, if a profile happens to exist.

        Read here rather than written: onboarding does not create or edit a profile, it
        only reports a name the farmer may already have one elsewhere.
        """
        profile = await self._profiles.find_by_user_id(user_id)
        return profile.display_name if profile is not None else None
