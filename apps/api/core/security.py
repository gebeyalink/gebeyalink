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
