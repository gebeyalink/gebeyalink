"""Wiring that turns configured parts into services a route can ask for.

Routes depend on services, never on a database session or a Redis client directly. That
keeps the business rules in one place and means a route can be read without knowing how a
connection is obtained.

The messaging backend and the settings are held on the application object rather than built
per request. The backend in particular must be one instance: it is the thing that remembers
what was sent, and a fresh instance per request would remember nothing.
"""

from __future__ import annotations

import uuid
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
from apps.api.core.staff_secrets import StaffSecretCipher
from apps.api.db.session import get_session_factory
from apps.api.models.staff_user import StaffRole, StaffUser
from apps.api.providers.sms import SmsProvider
from apps.api.repositories.action_token_repository import ActionTokenRepository
from apps.api.repositories.agent_assignment_repository import AgentAssignmentRepository
from apps.api.repositories.audit_log_repository import AuditLogRepository
from apps.api.repositories.crop_repository import CropRepository
from apps.api.repositories.kyc_case_repository import KycCaseRepository
from apps.api.repositories.listing_repository import ListingRepository
from apps.api.repositories.notification_repository import NotificationRepository
from apps.api.repositories.offer_repository import OfferRepository
from apps.api.repositories.order_repository import OrderRepository
from apps.api.repositories.price_index_repository import PriceIndexRepository
from apps.api.repositories.profile_repository import ProfileRepository
from apps.api.repositories.region_repository import RegionRepository
from apps.api.repositories.reservation_repository import ReservationRepository
from apps.api.repositories.search_repository import SearchRepository
from apps.api.repositories.session_repository import SessionRepository
from apps.api.repositories.staff_session_repository import StaffSessionRepository
from apps.api.repositories.staff_user_repository import StaffUserRepository
from apps.api.repositories.user_repository import UserRepository
from apps.api.repositories.user_role_repository import UserRoleRepository
from apps.api.repositories.verification_code_repository import VerificationCodeRepository
from apps.api.repositories.woreda_repository import WoredaRepository
from apps.api.services.admin_service import (
    SCOPE_FULL,
    AdminService,
    StaffAuthService,
)
from apps.api.services.agent_service import AgentRoleAdmin, AgentService
from apps.api.services.auth_service import AuthService
from apps.api.services.kyc_service import KycService
from apps.api.services.listing_service import ListingService
from apps.api.services.offer_service import OfferService
from apps.api.services.order_service import OrderService
from apps.api.services.otp_service import OtpService
from apps.api.services.profile_service import ProfileService
from apps.api.services.rate_limit_service import RateLimitService
from apps.api.services.redis_challenge_store import AgentChallengeRepository
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


def get_kyc_service(db_session: db_session_dep) -> KycService:
    """Opening a tier-upgrade case for the signed-in account.

    One session for the case and the profile write, on the same reasoning the profile service
    shares its own: a submission that carries a tax number writes the case and the number
    together, and separate sessions would let one land without the other.
    """
    return KycService(
        users=UserRepository(db_session),
        cases=KycCaseRepository(db_session),
        profiles=ProfileRepository(db_session),
    )


KycServiceDep = Annotated[KycService, Depends(get_kyc_service)]


def get_listing_service(db_session: db_session_dep) -> ListingService:
    """Postings, their lifecycle, and the message a USSD publish owes its seller.

    One session for all five repositories, so that a listing, the index row read while creating
    it, the reservations that decide whether it may be edited and the confirmation AC-3.1 owes
    are all seen in the same transaction as the listing itself.
    """
    return ListingService(
        listings=ListingRepository(db_session),
        price_indices=PriceIndexRepository(db_session),
        reservations=ReservationRepository(db_session),
        idempotency=IdempotencyStore(get_client()),
        users=UserRepository(db_session),
        notifications=NotificationRepository(db_session),
    )


ListingServiceDep = Annotated[ListingService, Depends(get_listing_service)]


def get_agent_service(
    db_session: db_session_dep,
    settings: settings_dep,
    sms: sms_dep,
) -> AgentService:
    """Agents, the farmers they carry, and the postings made on a farmer's behalf.

    One session for every repository and both sub-services, for the reason the listing
    service shares its own: onboarding a farmer writes an account and a relationship that
    must land together or not at all, and a delegated listing is written by the *same*
    listing service every other door uses, so it cannot drift into a second, looser path.

    The OTP service is built here exactly as it is for sign-in, over the same Redis and the
    same messaging backend, so the code sent to a farmer's phone is issued and checked by the
    one implementation rather than a parallel one.
    """
    return AgentService(
        users=UserRepository(db_session),
        assignments=AgentAssignmentRepository(db_session),
        challenges=AgentChallengeRepository(get_client()),
        profiles=ProfileRepository(db_session),
        otp=get_otp_service(get_client(), settings, sms),
        listings=get_listing_service(db_session),
        audit=AuditLogRepository(db_session),
        settings=settings,
    )


AgentServiceDep = Annotated[AgentService, Depends(get_agent_service)]


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

    One session for all five repositories, and that is not tidiness. Placing a bid writes the offer
    and the message the seller is owed about it, and reads the seller who must be told and the crop
    the message names; the whole of that is one commit or none of it, so everything that writes
    beside the offer shares the session the offer is written on.

    Both payment-mode flags are read here and handed in as values, on the same reasoning as the
    order service below: the service decides which modes exist without reaching back into
    configuration for a second answer.
    """
    return OfferService(
        offers=OfferRepository(db_session),
        listings=ListingRepository(db_session),
        notifications=NotificationRepository(db_session),
        users=UserRepository(db_session),
        crops=CropRepository(db_session),
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
        notifications=NotificationRepository(db_session),
        users=UserRepository(db_session),
        crops=CropRepository(db_session),
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
        listing_service=get_listing_service(db_session),
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


# -- staff -------------------------------------------------------------------


def _secret_cipher(settings: Settings) -> StaffSecretCipher:
    """The Fernet cipher the staff TOTP secrets are encrypted with."""
    return StaffSecretCipher(settings.totp_encryption_key.get_secret_value())


def get_staff_auth_service(db_session: db_session_dep, settings: settings_dep) -> StaffAuthService:
    """Sign-in, password change and logout for staff accounts."""
    return StaffAuthService(
        staff=StaffUserRepository(db_session),
        sessions=StaffSessionRepository(db_session),
        audit=AuditLogRepository(db_session),
        tokens=_token_service(settings),
        secrets=_secret_cipher(settings),
        settings=settings,
    )


StaffAuthServiceDep = Annotated[StaffAuthService, Depends(get_staff_auth_service)]


def get_admin_service(db_session: db_session_dep, settings: settings_dep) -> AdminService:
    """Staff administration, the KYC queue, and the audited agent decisions.

    One session for every repository on purpose: a decision and the audit row that records it
    must land together or not at all, so they share the transaction. The agent admin service
    built here is the *same* one the CLI builds, so the two write one audit shape.
    """
    audit = AuditLogRepository(db_session)
    agent_admin = AgentRoleAdmin(
        users=UserRepository(db_session),
        user_roles=UserRoleRepository(db_session),
        assignments=AgentAssignmentRepository(db_session),
        audit=audit,
    )
    return AdminService(
        staff=StaffUserRepository(db_session),
        sessions=StaffSessionRepository(db_session),
        users=UserRepository(db_session),
        cases=KycCaseRepository(db_session),
        user_roles=UserRoleRepository(db_session),
        assignments=AgentAssignmentRepository(db_session),
        agent_admin=agent_admin,
        audit=audit,
        secrets=_secret_cipher(settings),
        settings=settings,
    )


AdminServiceDep = Annotated[AdminService, Depends(get_admin_service)]


async def require_staff(
    settings: settings_dep,
    service: StaffAuthServiceDep,
    authorization: Annotated[str | None, Header()] = None,
) -> _StaffContext:
    """Identify the signed-in staff caller.

    The token is verified first, then the *session row* it names is loaded: the token says a
    session existed when it was minted, and the row says whether it is still live now. A
    disabled account, a role change, a password change and a logout all end a token that is
    otherwise well signed, and this is where they are honoured.

    This dependency carries no role gate: a role belongs to a route, and the routes declare it
    through the wrappers below. Keeping the role out of *this* signature matters for a subtler
    reason than tidiness — a plain parameter on a dependency function is one FastAPI tries to
    read from the request body, so a ``roles`` argument here would demand a body field the
    routes do not have. The gate lives in the wrappers, where it is a check rather than a
    parameter.
    """
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise ApiError(ErrorCode.AUTH_REQUIRED, details={"reason": "no_bearer_token"})
    try:
        verified = _token_service(settings).verify_staff(token)
    except TokenError as exc:
        raise ApiError(ErrorCode.FORBIDDEN, details={"reason": "unreadable"}) from exc

    session = await service.resolve_session(
        staff_user_id=verified.staff_user_id, session_id=verified.session_id
    )
    return _StaffContext(
        staff=session.staff,
        session_id=session.session_id,
        scope=verified.scope,
    )


class _StaffContext:
    """The staff actor a route sees: who they are, their session, and their token's scope."""

    __slots__ = ("scope", "session_id", "staff")

    def __init__(self, *, staff: StaffUser, session_id: uuid.UUID, scope: str) -> None:
        self.staff = staff
        self.session_id = session_id
        self.scope = scope


#: The caller on the sign-in handshake routes: any scope, any role.
CurrentStaffDep = Annotated[_StaffContext, Depends(require_staff)]


async def require_full_staff(
    settings: settings_dep,
    service: StaffAuthServiceDep,
    authorization: Annotated[str | None, Header()] = None,
) -> _StaffContext:
    """A staff caller whose token is a full one, not the change-password-only scope."""
    context = await require_staff(settings=settings, service=service, authorization=authorization)
    if context.scope != SCOPE_FULL:
        raise ApiError(ErrorCode.FORBIDDEN, details={"reason": "change_password_required"})
    return context


FullStaffDep = Annotated[_StaffContext, Depends(require_full_staff)]


def require_kyc_reviewer(context: FullStaffDep) -> _StaffContext:
    """A full-scoped staff caller whose role may work the KYC queue (VERIFIER or ADMIN)."""
    if context.staff.role not in (StaffRole.VERIFIER, StaffRole.ADMIN):
        raise ApiError(ErrorCode.FORBIDDEN, details={"reason": "role_required"})
    return context


KycReviewerDep = Annotated[_StaffContext, Depends(require_kyc_reviewer)]


def require_admin_staff(context: FullStaffDep) -> _StaffContext:
    """A full-scoped staff caller whose role is ADMIN."""
    if context.staff.role != StaffRole.ADMIN:
        raise ApiError(ErrorCode.FORBIDDEN, details={"reason": "role_required"})
    return context


AdminOnlyDep = Annotated[_StaffContext, Depends(require_admin_staff)]
