"""The rules for asking to be moved to a higher verification tier.

§6 governs how much a trader may trade, and the amount tracks ``users.kyc_tier`` rather than
the label on the account. Moving a tier is therefore two acts with two actors: the account
holder *submits* evidence and asks for a tier, and a verifier *reviews* that evidence and
decides. This service is the first act. It writes one ``PENDING`` case and — for a business
account — the tax number the case carries. It never moves a tier: approval is the admin
queue's, and a submission that changed a tier would make the review a formality.

Three rules shape a submission.

    The tier asked for must be exactly the next rung up. A request for the tier an account
    already has is not a request to verify anything, and a request that skips a rung would hand
    a reviewer a document to check against a level the account never earned; both are refused
    rather than stored for a reviewer to read and wonder at. §6's ladder is ordered, so "the
    next rung" is a comparison of positions and not of the labels alphabetically.

    One waiting case at a time. A trader with a ``PENDING`` case may not open a second, because
    two waiting cases for one account is two reviewers deciding the same question. A decided
    case — approved or rejected — is history and does not block a fresh ask, which is what lets
    a rejected trader try again and an approved one ask for the next tier up.

    A tax number travels with the case. §11.1's ``BUSINESS_LICENSE`` path is where a business
    account records its TIN, so a submission that carries one writes it onto the account's
    profile in the *same* transaction as the case. If the case cannot be written, the tax
    number is not either, and the two are never seen apart.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from apps.api.models.kyc_case import KycCase, KycDocType, KycTier
from apps.api.models.profile import Profile
from apps.api.models.user import VerificationTier
from apps.api.repositories.kyc_case_repository import KycCaseRepository
from apps.api.repositories.profile_repository import ProfileRepository
from apps.api.repositories.user_repository import UserRepository
from packages.shared.errors import ApiError, ErrorCode

#: §6's ladder, lowest first. The position in this tuple, not the label, is what "higher"
#: means; a request naming a tier the ladder does not hold is refused by the shape before it
#: reaches here, so this is the whole of the ordering the service reasons about.
TIER_LADDER: tuple[VerificationTier, ...] = (
    VerificationTier.TIER_0,
    VerificationTier.TIER_1,
    VerificationTier.TIER_2,
)

#: The tiers a trader may hold in the account's own vocabulary, keyed by the case's. The two
#: enums name the same ladder; one is the account's, one is the request's, and a case asking
#: for ``TIER_1`` moves the account to ``VerificationTier.TIER_1`` when it is approved.
_REQUEST_TO_ACCOUNT_TIER: dict[KycTier, VerificationTier] = {
    KycTier.TIER_1: VerificationTier.TIER_1,
    KycTier.TIER_2: VerificationTier.TIER_2,
}


@dataclass(frozen=True, slots=True)
class Submission:
    """A request to open a case, as the route read it off the wire."""

    tier_requested: KycTier
    doc_type: KycDocType
    doc_images: list[str]
    tax_identification_number: str | None = None


class KycService:
    """Opening a tier-upgrade case for the account that owns it."""

    def __init__(
        self,
        users: UserRepository,
        cases: KycCaseRepository,
        profiles: ProfileRepository,
    ) -> None:
        self._users = users
        self._cases = cases
        self._profiles = profiles

    async def submit(self, user_id: uuid.UUID, submission: Submission) -> KycCase:
        """Open a waiting case for this account, or refuse and write nothing.

        Every check runs before anything is written, so a refusal leaves no row behind: the
        account's tier is read, the tier asked for is compared to it, and only then is the
        waiting case looked for. A refusal is a refusal, not a half-written submission.
        """
        user = await self._users.find_by_id(user_id)
        if user is None:  # pragma: no cover - a valid token names an account that signed in
            raise ApiError(ErrorCode.NOT_FOUND, details={"resource": "user"})

        _require_next_tier(user.kyc_tier, submission.tier_requested)

        waiting = await self._cases.find_pending_by_user(user_id)
        if waiting is not None:
            # One case at a time. The refusal reuses the catalog's ``KYC_PENDING`` — a 409
            # raised for exactly this state — rather than inventing a code: R2 says this slice
            # adds no code, and a case that already waits is a conflict of state, not a
            # malformed request.
            raise ApiError(
                ErrorCode.KYC_PENDING,
                details={"resource": "kyc_case", "reason": "case_already_pending"},
            )

        case = await self._cases.add(
            KycCase(
                user_id=user_id,
                tier_requested=submission.tier_requested,
                doc_type=submission.doc_type,
                doc_images=submission.doc_images,
            )
        )

        if submission.tax_identification_number is not None:
            # Written on the same session as the case, above, and committed by the one commit
            # below: the tax number and the case it arrived with land together or not at all.
            await self._write_tin(user_id, submission.tax_identification_number)

        await self._cases.commit()
        return case

    async def _write_tin(self, user_id: uuid.UUID, tin: str) -> None:
        """Record the business account's tax number on its profile.

        The profile may not exist yet — an account is not required to have filled anything in
        before it asks to be verified — so the row is created if the account has none. This is
        the merge the profile endpoint performs, narrowed to the one field this submission
        carries, so a profile that already holds a name and an address keeps both.
        """
        existing = await self._profiles.find_by_user_id(user_id)
        if existing is None:
            await self._profiles.create(Profile(user_id=user_id, tax_identification_number=tin))
            return
        await self._profiles.save_changes(existing, {"tax_identification_number": tin})


def _require_next_tier(held: VerificationTier, requested: KycTier) -> None:
    """Refuse a request that is not exactly the next rung up from the one held.

    §6's ladder is climbed a rung at a time, so the rule is not "higher" but "the next one":
    ``TIER_1`` from a ``TIER_0`` account is the step, ``TIER_2`` from the same account is a
    skip, and ``TIER_1`` from a ``TIER_1`` account is not a step at all. A skip is refused
    because the document a reviewer is handed would be checked against a level the account
    never earned; a repeat is refused because it is not a request to verify anything. Both are
    the caller's field, so both are a validation error.
    """
    wanted = _REQUEST_TO_ACCOUNT_TIER[requested]
    held_position = TIER_LADDER.index(held)
    if TIER_LADDER.index(wanted) != held_position + 1:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            details={"fields": {"tier_requested": "must_be_the_next_tier_up"}},
        )
