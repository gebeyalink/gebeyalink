"""Reading and changing one's own profile.

Two endpoints, and only ever the caller's own row. There is no path parameter because there
is no other profile to ask for: a profile describes the person holding the token, so the
identifier is the token.
"""

from __future__ import annotations

from fastapi import APIRouter, status

from apps.api.dependencies import AccessTokenDep, KycServiceDep, ProfileServiceDep
from apps.api.models.profile import Profile
from apps.api.models.user import User
from apps.api.schemas.kyc import KycCaseResponse, KycSubmissionBody
from apps.api.schemas.profile import ProfileResponse, ProfileUpdateBody
from apps.api.services.kyc_service import Submission
from apps.api.services.profile_service import ProfileChanges

router = APIRouter(prefix="", tags=["Profile"])


def _as_response(user: User, profile: Profile | None) -> ProfileResponse:
    """Shape the account and its profile into the one body the contract describes.

    A profile that was never written answers as nulls rather than as an error, so the fields
    are read with a default rather than requiring a row.
    """
    return ProfileResponse.model_validate(
        {
            **{field: getattr(user, field) for field in _ACCOUNT_FIELDS},
            # Nobody holds a second activity yet, so this is empty for every trader rather
            # than looked up. It is stated rather than inferred so that adding a second
            # activity has somewhere obvious to be reported from.
            "additional_roles": [],
            **{field: getattr(profile, field, None) for field in _PROFILE_FIELDS},
        }
    )


_ACCOUNT_FIELDS = ("id", "phone_e164", "role", "language", "kyc_tier", "status", "terms_version")

_PROFILE_FIELDS = (
    "display_name",
    "region_code",
    "woreda_id",
    "kebele",
    "business_name",
    "license_no",
    "tax_identification_number",
)


@router.get("/me", response_model=ProfileResponse)
async def read_own_profile(
    caller: AccessTokenDep,
    profiles: ProfileServiceDep,
) -> ProfileResponse:
    """The caller's own account and profile.

    Answered even for a trader who has never filled anything in: what they have not said
    about themselves comes back as null rather than as a request to say it.
    """
    user, profile = await profiles.read(caller.user_id)
    return _as_response(user, profile)


@router.patch("/me", response_model=ProfileResponse, status_code=status.HTTP_200_OK)
async def update_own_profile(
    body: ProfileUpdateBody,
    caller: AccessTokenDep,
    profiles: ProfileServiceDep,
) -> ProfileResponse:
    """Change some of the caller's own fields.

    A merge, not a replacement: each field that arrived is written and each field that did
    not is left exactly as it was, which is what lets an offline client holding a stale copy
    send the one thing the user changed without knowing what we hold for the rest.
    """
    changes = ProfileChanges(
        display_name=body.display_name,
        region_code=body.region_code,
        woreda_id=body.woreda_id,
        kebele=body.kebele,
        language=body.language,
        business_name=body.business_name,
        license_no=body.license_no,
        tax_identification_number=body.tax_identification_number,
        gps=None if body.gps is None else (body.gps.latitude, body.gps.longitude),
    )
    user, profile = await profiles.update(caller.user_id, changes)
    return _as_response(user, profile)


@router.post(
    "/me/kyc",
    response_model=KycCaseResponse,
    status_code=status.HTTP_201_CREATED,
)
async def submit_kyc_case(
    body: KycSubmissionBody,
    caller: AccessTokenDep,
    kyc: KycServiceDep,
) -> KycCaseResponse:
    """Open a tier-upgrade case for the caller.

    Any authenticated account may ask to be verified; the tier is not moved here. The case
    waits for a verifier's decision, which is the admin queue's and lands in a later slice, so
    what a submission returns is the waiting case's receipt rather than a new tier.
    """
    case = await kyc.submit(
        caller.user_id,
        Submission(
            tier_requested=body.tier_requested,
            doc_type=body.doc_type,
            doc_images=body.doc_images,
            tax_identification_number=body.tax_identification_number,
        ),
    )
    return KycCaseResponse.model_validate(case)
