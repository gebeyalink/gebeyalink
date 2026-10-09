"""Signing in: requesting a code, exchanging it for a session, rotating, and signing out.

These are the only routes in the platform that need no credentials to call, which shapes
everything about them. They are also the routes a stranger will hammer, so each one counts
against the caller's address before doing any work.

Requesting a code never reveals whether a number already has an account. Answering either
way would let a caller map which numbers are registered, and that list is worth more than
the codes themselves. The answer is the same in both cases: a code is on its way.
"""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import APIRouter, Depends, Header, Request, Response, status

from apps.api.core.config import Settings
from apps.api.core.trace import set_language, set_language_from_header
from apps.api.dependencies import (
    get_auth_service,
    get_otp_service,
    get_rate_limit_service,
    get_settings,
    get_sms_provider,
    redis_dep,
)
from apps.api.providers.sms import SmsProvider
from apps.api.schemas.auth import (
    OtpRequestAccepted,
    OtpRequestBody,
    OtpVerifyBody,
    RefreshTokenResponse,
    TokenPairResponse,
    UserSummary,
)
from apps.api.services.auth_service import AuthService
from apps.api.services.otp_service import OtpService
from apps.api.services.rate_limit_service import RateLimitService
from packages.shared.errors import ApiError, ErrorCode
from packages.shared.i18n import resolve_language
from packages.shared.phone import is_valid_ethiopian_mobile
from packages.ussd.session import RedisLike, SessionStore

router = APIRouter(prefix="/auth", tags=["Auth"])

SettingsDep = Annotated[Settings, Depends(get_settings)]
SmsDep = Annotated[SmsProvider, Depends(get_sms_provider)]
OtpDep = Annotated[OtpService, Depends(get_otp_service)]
AuthDep = Annotated[AuthService, Depends(get_auth_service)]
RateLimitDep = Annotated[RateLimitService, Depends(get_rate_limit_service)]


def caller_address(request: Request) -> str:
    """The address a call appears to come from.

    The forwarding header is honoured because the platform sits behind a load balancer that
    sets it. It is only trustworthy if the edge in front of the API is configured to strip
    client-supplied values; otherwise a caller could sidestep every per-address limit by
    inventing a new header on each request.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _bearer_token(authorization: str | None) -> str:
    """Pull the token out of an authorization header."""
    if not authorization:
        raise ApiError(ErrorCode.TOKEN_INVALID, details={"reason": "missing_credentials"})
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise ApiError(ErrorCode.TOKEN_INVALID, details={"reason": "malformed_credentials"})
    return token


async def _enforce_sign_in_rate_limit(
    request: Request, limits: RateLimitDep, settings: SettingsDep
) -> None:
    """Hold every route in this router to the per-minute sign-in ceiling."""
    decision = await limits.check_sign_in_calls(
        caller_address(request), limit=settings.auth_endpoint_limit_per_minute_per_ip
    )
    if not decision.allowed:
        raise ApiError(
            ErrorCode.RATE_LIMITED,
            details={
                "limit": decision.limit,
                "scope": "sign_in_per_minute_per_address",
                "retry_after_seconds": decision.retry_after_seconds,
            },
        )


RateLimited = Depends(_enforce_sign_in_rate_limit)


@router.post(
    "/otp/request",
    response_model=OtpRequestAccepted,
    status_code=status.HTTP_200_OK,
    dependencies=[RateLimited],
)
async def request_otp(
    request: Request,
    body: OtpRequestBody,
    settings: SettingsDep,
    otp: OtpDep,
    limits: RateLimitDep,
    sms: SmsDep,
    accept_language: Annotated[str | None, Header()] = None,
) -> OtpRequestAccepted:
    """Send a verification code to a number."""
    set_language_from_header(accept_language)
    if not is_valid_ethiopian_mobile(body.phone_e164):
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            details={"fields": {"phone_e164": "not_a_valid_ethiopian_mobile_number"}},
        )

    address = caller_address(request)
    phone_limit = await limits.check_codes_to_number(
        body.phone_e164, limit=settings.otp_daily_send_limit_per_phone
    )
    address_limit = await limits.check_codes_from_address(
        address, limit=settings.otp_daily_send_limit_per_ip
    )
    if not phone_limit.allowed or not address_limit.allowed:
        exceeded = phone_limit if not phone_limit.allowed else address_limit
        raise ApiError(
            ErrorCode.RATE_LIMITED,
            details={
                "limit": exceeded.limit,
                "scope": "codes_per_day",
                "retry_after_seconds": exceeded.retry_after_seconds,
            },
        )

    cooldown_remaining = await otp.resend_wait_remaining(body.phone_e164)
    if cooldown_remaining > 0:
        raise ApiError(
            ErrorCode.RATE_LIMITED,
            details={
                "scope": "resend_cooldown",
                "retry_after_seconds": cooldown_remaining,
            },
        )

    language = resolve_language(accept_language)
    set_language(language)
    await otp.send_code(body.phone_e164, language)
    await limits.record_code_sent(
        phone_e164=body.phone_e164,
        client_ip=address,
        ttl_seconds=24 * 60 * 60,
    )
    return OtpRequestAccepted(
        expires_in_seconds=settings.otp_ttl_seconds,
        resend_after_seconds=settings.otp_resend_cooldown_seconds,
    )


@router.post(
    "/otp/verify",
    response_model=TokenPairResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[RateLimited],
)
async def verify_otp(
    body: OtpVerifyBody,
    auth: AuthDep,
    settings: SettingsDep,
    redis: redis_dep,
    accept_language: Annotated[str | None, Header()] = None,
) -> TokenPairResponse:
    """Exchange a verification code for a session, registering the trader if new."""
    header_language = resolve_language(accept_language)
    set_language(header_language)

    # A trader who says which language they want is making a choice worth keeping; a caller
    # that merely reads in another language is not. The service draws that distinction.
    language = body.language or header_language

    # A handset that already chose a language on the USSD channel keeps that choice on first
    # sign-in: the stored value travels in as ``stored_language`` and the service adopts it
    # until the app itself states something newer.
    stored_language = await SessionStore(cast(RedisLike, redis)).load_language(body.phone_e164)

    signed_in = await auth.sign_in_with_code(
        phone_e164=body.phone_e164,
        code=body.code,
        terms_version=body.terms_version,
        language=language,
        stated_language=body.language,
        stored_language=stored_language,
    )
    user = signed_in.user
    set_language(user.language)

    return TokenPairResponse(
        access_token=signed_in.access_token,
        access_expires_at=signed_in.access_expires_at,
        refresh_token=signed_in.refresh_token,
        refresh_expires_at=signed_in.refresh_expires_at,
        user=UserSummary(
            id=user.id,
            phone_e164=user.phone_e164,
            role=user.role,
            language=user.language,
            kyc_tier=user.kyc_tier,
            status=user.status,
        ),
    )


@router.post(
    "/refresh",
    response_model=RefreshTokenResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[RateLimited],
)
async def rotate_refresh_token(
    auth: AuthDep,
    accept_language: Annotated[str | None, Header()] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> RefreshTokenResponse:
    """Trade a refresh token for a fresh pair.

    Each refresh token works exactly once. Presenting one that has already been traded
    closes every session on the account.
    """
    set_language_from_header(accept_language)
    pair = await auth.rotate_refresh_token(_bearer_token(authorization))
    return RefreshTokenResponse(
        access_token=pair.access_token,
        access_expires_at=pair.access_expires_at,
        refresh_token=pair.refresh_token,
        refresh_expires_at=pair.refresh_expires_at,
    )


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[RateLimited],
)
async def sign_out(
    auth: AuthDep,
    accept_language: Annotated[str | None, Header()] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> Response:
    """Close the session a refresh token belongs to."""
    set_language_from_header(accept_language)
    await auth.sign_out(_bearer_token(authorization))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
