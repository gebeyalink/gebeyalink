"""Issuing and checking the two tokens a signed-in session carries.

An access token is short-lived and checked on nearly every request, so it is self-contained
and stateless. A refresh token is longer-lived and therefore stateful: the database records
which token is currently valid for each session, which is what makes replay detectable.

Tokens are signed, never encrypted. Their contents are readable by anyone holding the token,
so they carry identifiers and nothing else — no phone number, no language preference, no
anything a third party could use. Those live in the account record.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import UUID

import jwt
from jwt.exceptions import InvalidTokenError

from apps.api.core.hashing import generate_random_token, hash_token

ACCESS_TOKEN_KIND: Final = "access"
REFRESH_TOKEN_KIND: Final = "refresh"
#: The kind stamped into a staff token. A distinct kind is what keeps a staff token from being
#: presented to a trader route and vice versa: both are signed with the same keys, and the kind
#: is the whole of what tells them apart.
STAFF_TOKEN_KIND: Final = "staff"


@dataclass(frozen=True, slots=True)
class TokenPair:
    """The two tokens handed to a client, plus what we keep to police rotation."""

    access_token: str
    access_expires_at: datetime
    refresh_token: str
    refresh_expires_at: datetime
    session_id: str
    refresh_token_hash: str


@dataclass(frozen=True, slots=True)
class VerifiedToken:
    """What we trust about a token after its signature checked out."""

    user_id: UUID
    session_id: str
    token_kind: str


@dataclass(frozen=True, slots=True)
class StaffToken:
    """A minted staff token and what it carries beyond the signature.

    ``scope`` is read back by the staff dependency and decides which routes a token may reach:
    ``full`` reaches everything its role allows, ``change_password_only`` reaches exactly the
    change-password route. ``role`` is carried so a route can refuse on it without a second read,
    though the dependency still loads the row — a role in a token is a claim as of signing time,
    and the row is the truth.
    """

    access_token: str
    expires_at: datetime
    session_id: UUID
    scope: str
    role: str


@dataclass(frozen=True, slots=True)
class VerifiedStaffToken:
    """What we trust about a staff token after its signature checked out."""

    staff_user_id: UUID
    session_id: UUID
    scope: str
    role: str


class TokenError(Exception):
    """The token is unusable: malformed, expired, signed by someone else, or the wrong kind."""


class TokenService:
    """Mints and verifies the access and refresh halves of a session."""

    def __init__(
        self,
        private_key: str,
        public_key: str,
        key_id: str,
        access_lifetime_seconds: int,
        refresh_lifetime_seconds: int,
    ) -> None:
        self._private_key = private_key
        self._public_key = public_key
        self._key_id = key_id
        self._access_lifetime = access_lifetime_seconds
        self._refresh_lifetime = refresh_lifetime_seconds

    def _encode(
        self, claims: dict[str, Any], lifetime: timedelta, kind: str
    ) -> tuple[str, datetime]:
        now = datetime.now(UTC)
        expires_at = now + lifetime
        payload = {
            **claims,
            "kind": kind,
            "kid": self._key_id,
            "iat": int(now.timestamp()),
            "exp": int(expires_at.timestamp()),
        }
        token = jwt.encode(
            payload,
            self._private_key,
            algorithm="RS256",
            headers={"kid": self._key_id},
        )
        return token, expires_at

    def issue(self, user_id: UUID, session_id: str | None = None) -> TokenPair:
        """Open a new session, or rotate the refresh half of an existing one.

        Every refresh half is single-use: the caller stores the hash it receives here and
        refuses the same token again on the next rotation.

        The nonce is what makes that workable. Without it two tokens minted for the same
        session within the same second would carry identical claims and sign to the same
        string, so a rotation would hand back the token it was just given and the record of
        what was already spent could not tell them apart.
        """
        sid = session_id or generate_random_token(16)
        access_token, access_expires_at = self._encode(
            {"sub": str(user_id), "sid": sid, "nonce": generate_random_token(16)},
            timedelta(seconds=self._access_lifetime),
            ACCESS_TOKEN_KIND,
        )
        refresh_token, refresh_expires_at = self._encode(
            {"sub": str(user_id), "sid": sid, "nonce": generate_random_token(16)},
            timedelta(seconds=self._refresh_lifetime),
            REFRESH_TOKEN_KIND,
        )
        # Only the digest is ever written down. No salt: the token is a signed,
        # high-entropy string, so there is nothing to brute-force and nothing for a salt to
        # protect — unlike a verification code, which a person chooses out of a million.
        return TokenPair(
            access_token=access_token,
            access_expires_at=access_expires_at,
            refresh_token=refresh_token,
            refresh_expires_at=refresh_expires_at,
            session_id=sid,
            refresh_token_hash=hash_token(refresh_token),
        )

    def verify(self, token: str, expected_kind: str) -> VerifiedToken:
        """Check a token's signature, lifetime, and purpose.

        A token presented for the wrong purpose is rejected rather than honoured, so an
        access token can never be traded in for a longer-lived one.
        """
        try:
            claims = jwt.decode(
                token,
                self._public_key,
                algorithms=["RS256"],
                options={"require": ["exp", "sub", "kind"]},
            )
        except InvalidTokenError as exc:
            raise TokenError("token rejected") from exc

        if claims.get("kind") != expected_kind:
            raise TokenError("token presented for the wrong purpose")

        try:
            user_id = UUID(str(claims["sub"]))
            session_id = str(claims["sid"])
        except (KeyError, ValueError) as exc:
            raise TokenError("token is missing its subject") from exc

        return VerifiedToken(user_id=user_id, session_id=session_id, token_kind=expected_kind)

    def issue_staff(
        self,
        *,
        staff_user_id: UUID,
        session_id: UUID,
        role: str,
        scope: str,
        lifetime_seconds: int,
    ) -> StaffToken:
        """Mint a staff token over the same keys a trader token is signed with.

        The lifetime is passed in rather than read from ``self._access_lifetime``: a staff
        session's length is its own setting (``admin_jwt_ttl_seconds``), and the row written
        beside this token is the authority on it. The token's ``exp`` is a second, independent
        copy of the same moment.
        """
        token, expires_at = self._encode(
            {
                "sub": str(staff_user_id),
                "sid": str(session_id),
                "role": role,
                "scope": scope,
                "nonce": generate_random_token(16),
            },
            timedelta(seconds=lifetime_seconds),
            STAFF_TOKEN_KIND,
        )
        return StaffToken(
            access_token=token,
            expires_at=expires_at,
            session_id=session_id,
            scope=scope,
            role=role,
        )

    def verify_staff(self, token: str) -> VerifiedStaffToken:
        """Check a staff token's signature, lifetime, kind, and the claims a staff route needs.

        A trader token presented here fails on ``kind``, exactly as a staff token presented to a
        trader route would: the two are signed with the same key and told apart by what they say
        they are.
        """
        try:
            claims = jwt.decode(
                token,
                self._public_key,
                algorithms=["RS256"],
                options={"require": ["exp", "sub", "kind", "sid", "role", "scope"]},
            )
        except InvalidTokenError as exc:
            raise TokenError("staff token rejected") from exc

        if claims.get("kind") != STAFF_TOKEN_KIND:
            raise TokenError("token presented for the wrong purpose")

        try:
            staff_user_id = UUID(str(claims["sub"]))
            session_id = UUID(str(claims["sid"]))
            scope = str(claims["scope"])
            role = str(claims["role"])
        except (KeyError, ValueError) as exc:
            raise TokenError("staff token is missing its subject") from exc

        return VerifiedStaffToken(
            staff_user_id=staff_user_id,
            session_id=session_id,
            scope=scope,
            role=role,
        )
