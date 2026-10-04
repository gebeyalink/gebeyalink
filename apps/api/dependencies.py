"""Wiring that turns configured parts into services a route can ask for.

Routes depend on services, never on a database session or a Redis client directly. That
keeps the business rules in one place and means a route can be read without knowing how a
connection is obtained.

The messaging backend and the settings are held on the application object rather than built
per request. The backend in particular must be one instance: it is the thing that remembers
what was sent, and a fresh instance per request would remember nothing.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Header, Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.core.config import Settings
from apps.api.core.redis_client import get_client
from apps.api.core.security import ACCESS_TOKEN_KIND, TokenError, TokenService, VerifiedToken
from apps.api.db.session import get_session_factory
from apps.api.providers.sms import SmsProvider
from apps.api.repositories.listing_repository import ListingRepository
from apps.api.repositories.price_index_repository import PriceIndexRepository
from apps.api.repositories.profile_repository import ProfileRepository
from apps.api.repositories.region_repository import RegionRepository
from apps.api.repositories.session_repository import SessionRepository
from apps.api.repositories.user_repository import UserRepository
from apps.api.repositories.verification_code_repository import VerificationCodeRepository
from apps.api.repositories.woreda_repository import WoredaRepository
from apps.api.services.auth_service import AuthService
from apps.api.services.listing_service import ListingService
from apps.api.services.otp_service import OtpService
from apps.api.services.profile_service import ProfileService
from apps.api.services.rate_limit_service import RateLimitService
from packages.shared.errors import ApiError, ErrorCode


def get_settings(request: Request) -> Settings:
    """The configuration this process booted with."""
    settings: Settings = request.app.state.settings
    return settings


def get_sms_provider(request: Request) -> SmsProvider:
    """The messaging backend for this process."""
    provider: SmsProvider = request.app.state.sms_provider
    return provider


async def get_redis() -> AsyncIterator[Redis]:
    """The store holding verification codes and live sessions."""
    yield get_client()


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """A database session for the length of one request."""
    factory = get_session_factory()
    async with factory() as session:
        yield session


db_session_dep = Annotated[AsyncSession, Depends(get_db_session)]


def get_rate_limit_service() -> RateLimitService:
    """Abuse counters for sign-in calls."""
    return RateLimitService(get_client())


redis_dep = Annotated[Redis, Depends(get_redis)]
sms_dep = Annotated[SmsProvider, Depends(get_sms_provider)]
settings_dep = Annotated[Settings, Depends(get_settings)]


def get_verification_code_repository(redis: redis_dep) -> VerificationCodeRepository:
    """Pending codes and the counters that police them."""
    return VerificationCodeRepository(redis)


def get_otp_service(redis: redis_dep, settings: settings_dep, sms: sms_dep) -> OtpService:
    """The verification-code lifecycle."""
    return OtpService(VerificationCodeRepository(redis), sms, settings)


otp_service_dep = Annotated[OtpService, Depends(get_otp_service)]


def _token_service(settings: Settings) -> TokenService:
    """The signer/verifier this process reads its key material from."""
    return TokenService(
        private_key=settings.signing_private_key.get_secret_value(),
        public_key=settings.signing_public_key.get_secret_value(),
        key_id=settings.signing_key_id,
        access_lifetime_seconds=settings.access_token_lifetime_seconds,
        refresh_lifetime_seconds=settings.refresh_token_lifetime_seconds,
    )


def get_auth_service(
    db_session: db_session_dep,
    otp: otp_service_dep,
    settings: settings_dep,
    redis: redis_dep,
) -> AuthService:
    """Registration, sign-in, and session rotation."""
    return AuthService(
        users=UserRepository(db_session),
        sessions=SessionRepository(redis),
        otp=otp,
        tokens=_token_service(settings),
        settings=settings,
    )


async def require_access_token(
    settings: settings_dep,
    authorization: Annotated[str | None, Header()] = None,
) -> VerifiedToken:
    """Identify the signed-in caller from the bearer token on the request.

    Reference data is only served to somebody already holding a session, so a request
    without a usable access token is refused before any query runs.
    """
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise ApiError(ErrorCode.AUTH_REQUIRED, details={"reason": "no_bearer_token"})
    try:
        return _token_service(settings).verify(token, ACCESS_TOKEN_KIND)
    except TokenError as exc:
        raise ApiError(ErrorCode.TOKEN_INVALID, details={"reason": "unreadable"}) from exc


def get_profile_service(db_session: db_session_dep) -> ProfileService:
    """A trader's own profile, and the reference data it is checked against.

    One session is shared by the four repositories on purpose: the request either writes the
    whole change or none of it, and separate sessions would let half of it land.
    """
    return ProfileService(
        profiles=ProfileRepository(db_session),
        users=UserRepository(db_session),
        regions=RegionRepository(db_session),
        woredas=WoredaRepository(db_session),
    )


DbSessionDep = db_session_dep
AccessTokenDep = Annotated[VerifiedToken, Depends(require_access_token)]
ProfileServiceDep = Annotated[ProfileService, Depends(get_profile_service)]


def get_listing_service(db_session: db_session_dep) -> ListingService:
    """Postings, their lifecycle, and the price index they are judged against.

    One session for both repositories, so that a listing and the index row read while
    creating it are seen in the same transaction as the listing itself.
    """
    return ListingService(
        listings=ListingRepository(db_session),
        price_indices=PriceIndexRepository(db_session),
    )


ListingServiceDep = Annotated[ListingService, Depends(get_listing_service)]
