"""Turning a verified phone number into a signed-in session.

There is no password anywhere in this flow. Possession of the number is the credential, and
the verification code is the proof of it — which is why proving it well matters more here
than it would behind a password screen.

Two behaviours are worth stating plainly, because both are easy to get subtly wrong:

Verifying against a number that already has an account signs that person back in rather
than creating a second account. Somebody who reinstalls the app, loses their handset, or
simply signs in again must land on their existing account with their history intact.

A refresh token is single-use. When one that has already been rotated comes back, we treat
it as evidence that somebody is holding a copy they should not have, and we close every
session the account has — not just the one that was replayed.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from apps.api.core.config import Settings
from apps.api.core.hashing import digests_match, hash_token
from apps.api.core.security import REFRESH_TOKEN_KIND, TokenError, TokenPair, TokenService
from apps.api.models.user import AccountStatus, PrimaryRole, User
from apps.api.repositories.session_repository import SessionRepository
from apps.api.repositories.user_repository import UserRepository
from apps.api.services.otp_service import OtpService
from packages.shared.errors import ApiError, ErrorCode
from packages.shared.i18n import Language

#: A new account starts as a seller. Which side of the market somebody trades on is settled
#: during onboarding, and until then this is the label that decides which journeys to offer.
DEFAULT_PRIMARY_ROLE = PrimaryRole.FARMER

#: Accounts that must not be handed a session, whatever else is true of them. Proving
#: ownership of the number is not a licence to trade: a suspended or closed account stays
#: shut, and the code that was just spent has to be replaced before trying again.
BLOCKED_ACCOUNT_STATUSES = frozenset({AccountStatus.SUSPENDED, AccountStatus.CLOSED})


@dataclass(frozen=True, slots=True)
class SignedInSession:
    """What a caller receives after proving they own a number."""

    user: User
    access_token: str
    access_expires_at: datetime
    refresh_token: str
    refresh_expires_at: datetime
    is_new_account: bool


class AuthService:
    """Registers, signs in, rotates sessions, and signs out."""

    def __init__(
        self,
        users: UserRepository,
        sessions: SessionRepository,
        otp: OtpService,
        tokens: TokenService,
        settings: Settings,
    ) -> None:
        self._users = users
        self._sessions = sessions
        self._otp = otp
        self._tokens = tokens
        self._settings = settings

    async def sign_in_with_code(
        self,
        phone_e164: str,
        code: str,
        terms_version: str,
        language: Language,
        stated_language: Language | None = None,
    ) -> SignedInSession:
        """Verify a code, then create or recover the account behind the number.

        The code is checked first and consumed whether or not an account exists, so a
        correct code cannot be replayed against a second registration.
        """
        await self._otp.check_code(phone_e164, code)

        accepted_at = datetime.now(UTC)
        existing = await self._users.find_by_phone(phone_e164)
        if existing is None:
            user = User(
                id=uuid.uuid4(),
                phone_e164=phone_e164,
                role=DEFAULT_PRIMARY_ROLE,
                language=language,
                status=AccountStatus.ACTIVE,
            )
            await self._users.add(user)
            is_new_account = True
        else:
            user = existing
            is_new_account = False
            if user.status in BLOCKED_ACCOUNT_STATUSES:
                raise ApiError(
                    ErrorCode.FORBIDDEN,
                    details={"account_status": user.status.value},
                )
            if stated_language is not None and stated_language != user.language:
                # A language the trader stated in the request is a change of preference and
                # is honoured. A language that arrived in a request header is not: it says
                # how this caller wants to read the reply, not what they want stored for
                # every message that follows.
                user.language = stated_language
                await self._users.save(user)

        # Recorded on every sign-in, not only the first. A revised version of the terms has
        # to be re-accepted, and the newest acceptance is what authorises trading.
        await self._users.record_terms_acceptance(
            user, terms_version=terms_version, accepted_at=accepted_at
        )

        pair = self._tokens.issue(user.id)
        await self._sessions.open(
            session_id=pair.session_id,
            user_id=user.id,
            refresh_token_hash=pair.refresh_token_hash,
            expires_at=pair.refresh_expires_at,
            ttl_seconds=self._settings.refresh_token_lifetime_seconds,
        )

        return SignedInSession(
            user=user,
            access_token=pair.access_token,
            access_expires_at=pair.access_expires_at,
            refresh_token=pair.refresh_token,
            refresh_expires_at=pair.refresh_expires_at,
            is_new_account=is_new_account,
        )

    async def rotate_refresh_token(self, refresh_token: str) -> TokenPair:
        """Exchange a refresh token for a new pair, or refuse and close the account's doors.

        Order matters here. The stored digest is compared before anything is issued, so a
        replayed token can never obtain a fresh one.
        """
        try:
            verified = self._tokens.verify(refresh_token, REFRESH_TOKEN_KIND)
        except TokenError as exc:
            raise ApiError(ErrorCode.TOKEN_INVALID, details={"reason": "unreadable"}) from exc

        record = await self._sessions.get(verified.session_id)
        if record is None:
            raise ApiError(ErrorCode.TOKEN_INVALID, details={"reason": "session_closed"})
        if record.user_id != verified.user_id:
            raise ApiError(ErrorCode.TOKEN_INVALID, details={"reason": "session_mismatch"})

        if not digests_match(hash_token(refresh_token), record.refresh_token_hash):
            # The signature was genuine and the token is unexpired, but it is not the token
            # this session currently accepts. That is a consumed token coming back: the only
            # two explanations are a stolen copy or a confused client, and both end the same
            # way. Closing only this session would leave any other session the holder has
            # already rotated wide open.
            await self._sessions.close_every_session_for(record.user_id)
            raise ApiError(ErrorCode.TOKEN_INVALID, details={"reason": "token_reuse"})

        pair = self._tokens.issue(record.user_id, session_id=record.session_id)
        await self._sessions.rotate(
            session_id=pair.session_id,
            user_id=record.user_id,
            refresh_token_hash=pair.refresh_token_hash,
            expires_at=pair.refresh_expires_at,
            ttl_seconds=self._settings.refresh_token_lifetime_seconds,
        )
        return pair

    async def sign_out(self, refresh_token: str) -> None:
        """Close the session a refresh token belongs to.

        A token that cannot be read is treated as already signed out. Signing out is
        something a person does on a device they may no longer control, so it must never
        be the request that fails.
        """
        try:
            verified = self._tokens.verify(refresh_token, REFRESH_TOKEN_KIND)
        except TokenError:
            return
        await self._sessions.close(verified.session_id)
