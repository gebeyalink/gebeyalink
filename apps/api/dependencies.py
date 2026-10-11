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
from typing import Annotated, cast

from fastapi import Depends, Header, Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.core.config import Settings
from apps.api.core.idempotency import IdempotencyStore
from apps.api.core.redis_client import get_client
from apps.api.core.security import ACCESS_TOKEN_KIND, TokenError, TokenService, VerifiedToken
from apps.api.core.short_codes import CrockfordOrderCode
from apps.api.db.session import get_session_factory
from apps.api.providers.sms import SmsProvider
from apps.api.repositories.action_token_repository import ActionTokenRepository
from apps.api.repositories.crop_repository import CropRepository
from apps.api.repositories.listing_repository import ListingRepository
from apps.api.repositories.offer_repository import OfferRepository
from apps.api.repositories.order_repository import OrderRepository
from apps.api.repositories.price_index_repository import PriceIndexRepository
from apps.api.repositories.profile_repository import ProfileRepository
from apps.api.repositories.region_repository import RegionRepository
from apps.api.repositories.reservation_repository import ReservationRepository
from apps.api.repositories.search_repository import SearchRepository
from apps.api.repositories.session_repository import SessionRepository
from apps.api.repositories.user_repository import UserRepository
from apps.api.repositories.verification_code_repository import VerificationCodeRepository
from apps.api.repositories.woreda_repository import WoredaRepository
from apps.api.services.auth_service import AuthService
from apps.api.services.listing_service import ListingService
from apps.api.services.offer_service import OfferService
from apps.api.services.order_service import OrderService
from apps.api.services.otp_service import OtpService
from apps.api.services.profile_service import ProfileService
from apps.api.services.rate_limit_service import RateLimitService
from apps.api.services.search_service import SearchService
from apps.api.services.sms_command_gateway import SmsCommandGateway
from apps.api.services.ussd_gateway import UssdGateway
from packages.shared.errors import ApiError, ErrorCode
from packages.ussd.session import RedisLike, SessionStore
from packages.ussd.trees import build_nodes
from packages.ussd.trees.model import Node


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

    One session for all three repositories, so that a listing, the index row read while creating
    it and the reservations that decide whether it may be edited are all seen in the same
    transaction as the listing itself.
    """
    return ListingService(
        listings=ListingRepository(db_session),
        price_indices=PriceIndexRepository(db_session),
        reservations=ReservationRepository(db_session),
        idempotency=IdempotencyStore(get_client()),
    )


ListingServiceDep = Annotated[ListingService, Depends(get_listing_service)]


def get_search_service(db_session: db_session_dep) -> SearchService:
    """Discovery: what is on the market, and what of it this trader may be shown.

    One session for all four repositories, for the reason the listing service has three:
    a page reads the listings, the photographs that go with them, the kilos an order is
    already holding against them, and whether the trader asking has a point to measure
    distance from, and those have to be the answers as of one moment rather than as of
    four.
    """
    return SearchService(
        search=SearchRepository(db_session),
        listings=ListingRepository(db_session),
        reservations=ReservationRepository(db_session),
        profiles=ProfileRepository(db_session),
    )


SearchServiceDep = Annotated[SearchService, Depends(get_search_service)]


def get_offer_service(db_session: db_session_dep, settings: settings_dep) -> OfferService:
    """Bids on a listing, and the listing they are bounded by.

    One session for both repositories, for the reason the listing service has one for three: a
    counter writes an offer and reads the listing it must satisfy the rules of, and those two
    statements have to describe the same listing.

    Both payment-mode flags are read here and handed in as values, on the same reasoning as the
    order service below: the service decides which modes exist without reaching back into
    configuration for a second answer.
    """
    return OfferService(
        offers=OfferRepository(db_session),
        listings=ListingRepository(db_session),
        idempotency=IdempotencyStore(get_client()),
        escrow_enabled=settings.escrow_enabled,
        cod_enabled=settings.cod_enabled,
    )


OfferServiceDep = Annotated[OfferService, Depends(get_offer_service)]


def get_order_service(
    db_session: db_session_dep, settings: settings_dep, redis: redis_dep
) -> OrderService:
    """Orders, the reservations that hold their stock, and the offers they came from.

    One session for all four repositories, and that is not tidiness. Accepting an offer writes an
    order, a reservation, the offer and possibly the listing's status, and reads the reservations
    it must be sure of; the whole of that is one commit or none of it, and separate sessions
    would let half of it land.

    The fee payer is read once here, from configuration, and handed to the service as a value
    rather than as a settings object, so that the service cannot reach back into configuration
    and read a different answer for a second order in the same process.
    """
    orders = OrderRepository(db_session)
    return OrderService(
        orders=orders,
        listings=ListingRepository(db_session),
        offers=OfferRepository(db_session),
        reservations=ReservationRepository(db_session),
        fee_payer=settings.fee_payer,
        cod_enabled=settings.cod_enabled,
        escrow_enabled=settings.escrow_enabled,
        codes=CrockfordOrderCode(orders.short_code_exists),
        idempotency=IdempotencyStore(get_client()),
        tolerance_pct=settings.transit_shrinkage_tolerance_pct,
    )


OrderServiceDep = Annotated[OrderService, Depends(get_order_service)]


def get_sms_gateway(
    db_session: db_session_dep, settings: settings_dep, redis: redis_dep
) -> SmsCommandGateway:
    """Everything the SMS command path needs, over the one session this request owns.

    The three services are built the same way their own endpoints build them and share this
    session, so a listing posted by text and one posted by app are written by the same code
    against the same transaction. Nothing here is SMS-specific: the gateway is a caller, not a
    second implementation.
    """
    return SmsCommandGateway(
        users=UserRepository(db_session),
        profiles=ProfileRepository(db_session),
        crops=CropRepository(db_session),
        price_indices=PriceIndexRepository(db_session),
        listings=ListingRepository(db_session),
        offers=OfferRepository(db_session),
        orders=OrderRepository(db_session),
        action_tokens=ActionTokenRepository(db_session),
        listing_service=get_listing_service(db_session),
        offer_service=get_offer_service(db_session, settings),
        order_service=get_order_service(db_session, settings, redis),
        settings=settings,
    )


SmsCommandGatewayDep = Annotated[SmsCommandGateway, Depends(get_sms_gateway)]


def get_ussd_gateway(
    db_session: db_session_dep, settings: settings_dep, redis: redis_dep
) -> UssdGateway:
    """Everything the USSD engine needs, over the one session this request owns.

    Built the way the SMS gateway is: the services are the same ones their own endpoints
    build, sharing this session, so a bid placed by dialling and one placed by app are
    written by the same code against the same transaction.
    """
    return UssdGateway(
        users=UserRepository(db_session),
        profiles=ProfileRepository(db_session),
        crops=CropRepository(db_session),
        price_indices=PriceIndexRepository(db_session),
        listings=ListingRepository(db_session),
        offers=OfferRepository(db_session),
        orders=OrderRepository(db_session),
        regions=RegionRepository(db_session),
        woredas=WoredaRepository(db_session),
        search=SearchRepository(db_session),
        offer_service=get_offer_service(db_session, settings),
        order_service=get_order_service(db_session, settings, redis),
        settings=settings,
    )


UssdGatewayDep = Annotated[UssdGateway, Depends(get_ussd_gateway)]


def get_ussd_session_store(redis: redis_dep) -> SessionStore:
    """The sessions, drafts and language choices of the USSD channel.

    ``redis.asyncio.Redis`` and the store's ``RedisLike`` protocol describe the same three
    calls with the same effect; only their parameter spellings differ, so a cast names that
    we are plugging the concrete client into the seam it was defined for.
    """
    return SessionStore(cast(RedisLike, redis))


UssdSessionStoreDep = Annotated[SessionStore, Depends(get_ussd_session_store)]


def get_ussd_engine() -> dict[str, Node]:
    """The whole menu graph, validated against the registry at build time."""
    return build_nodes()


ussd_engine_dep = Annotated[dict[str, Node], Depends(get_ussd_engine)]
