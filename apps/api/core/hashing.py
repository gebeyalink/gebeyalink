"""Turning secrets into things we can compare without keeping the secret.

Two different problems are solved here. A verification code is short-lived and low-value,
so it is stored as a salted digest: a stolen database copy reveals nothing an attacker
could brute-force cheaply. A refresh token is long-lived and bearer-like, so it is stored
as a digest too, and we keep a per-session copy so we can notice a token being replayed.

Digests are always compared in constant time. A comparison that returns early on the first
differing byte leaks the length of the match, which is enough to reconstruct a token one
attempt at a time.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

SALT_BYTES = 16
DIGEST_BYTES = 32


def generate_salt() -> str:
    """A fresh random salt. One per value, never reused across values."""
    return secrets.token_hex(SALT_BYTES)


def digest(value: str, salt: str) -> str:
    """Hash a value together with its salt.

    The salt is stored beside the digest, so this deliberately is not a password hash:
    verification codes live for minutes and are checked at most a handful of times, and
    their cost is bounded by the attempt counter rather than by memory-hard stretching.
    """
    return hashlib.sha256(f"{salt}:{value}".encode()).hexdigest()


def digests_match(left: str, right: str) -> bool:
    """Compare two digests without leaking how much of them matched."""
    return hmac.compare_digest(left, right)


def generate_random_token(nbytes: int = 32) -> str:
    """An opaque, unguessable identifier or secret."""
    return secrets.token_urlsafe(nbytes)


def hash_token(token: str) -> str:
    """Hash a bearer token for storage. No salt: the token is already high-entropy, and
    lookups have to match a digest without any per-token secret to hand."""
    return hashlib.sha256(token.encode()).hexdigest()
