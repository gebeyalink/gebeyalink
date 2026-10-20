"""Hashing and checking a staff member's password.

A staff account is the only account on the platform with a password. Trader accounts sign in
by a code sent to their own phone, so there is no password column on ``users`` and nothing for
this module to guard there; a staff member is a different kind of account (§5.2) with a
different sign-in, and this is its one secret.

``hashlib.scrypt`` is the whole implementation, and that is a deliberate choice rather than a
constraint. scrypt is memory-hard — it makes a guessing attack cost memory as well as time —
and it ships with the interpreter, so no third-party dependency and no supply chain enters the
one place a credential is handled. The parameters are OWASP's recommended figures.

The stored string carries its own parameters::

    scrypt$<N>$<r>$<p>$<base64 salt>$<base64 digest>

so that a parameter bump is readable from the value itself. ``verify`` parses the parameters
out of the stored string rather than assuming today's, which means an old hash keeps working
after the parameters are raised — and a caller can see, from the string alone, that it should
re-hash on the next successful login. The format needs no version column and no migration: the
row already says everything needed to read it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from typing import Final

#: OWASP's recommended scrypt cost: N=2**17, r=8, p=1. Expressed as integers rather than as the
#: power, because these reach the code through configuration and an environment variable that
#: carried ``2**17`` would be a string to evaluate rather than a number to read.
DEFAULT_N: Final = 2**17
DEFAULT_R: Final = 8
DEFAULT_P: Final = 1

#: Sixteen random bytes. A salt does not need to be secret, only unique per credential, and 16
#: bytes is the standard size.
SALT_BYTES: Final = 16

#: scrypt's own ceiling; longer keys are refused by the algorithm. 32 bytes is a standard digest
#: length and comfortably under that ceiling.
KEY_LENGTH: Final = 32

_ALGORITHM: Final = "scrypt"


def hash_password(
    plaintext: str,
    *,
    n: int = DEFAULT_N,
    r: int = DEFAULT_R,
    p: int = DEFAULT_P,
) -> str:
    """Hash a password into the self-describing string stored on the staff row.

    A fresh random salt every call, so the same password hashed twice produces two different
    strings — which is what stops a stolen table from revealing that two staff members chose
    the same password without being attacked at all.
    """
    salt = os.urandom(SALT_BYTES)
    digest = hashlib.scrypt(
        plaintext.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        dklen=KEY_LENGTH,
        maxmem=_maxmem(n, r, p),
    )
    return "$".join(
        (
            _ALGORITHM,
            str(n),
            str(r),
            str(p),
            base64.b64encode(salt).decode("ascii"),
            base64.b64encode(digest).decode("ascii"),
        )
    )


def verify_password(plaintext: str, stored: str) -> bool:
    """Whether a password matches a stored string, read on the string's own parameters.

    Returns False rather than raising for a string that is not one of ours: a stored value that
    cannot be parsed is a credential that cannot be honoured, which is the same outcome as a
    wrong password and not a reason to crash a sign-in.
    """
    parsed = _parse(stored)
    if parsed is None:
        return False
    n, r, p, salt, expected = parsed
    try:
        digest = hashlib.scrypt(
            plaintext.encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            dklen=len(expected),
            maxmem=_maxmem(n, r, p),
        )
    except ValueError:
        # Parameters in the stored string that the algorithm refuses (an absurd N, say) make
        # the credential unusable, which is a failed verification rather than an error.
        return False
    # Constant-time: a byte-by-byte comparison leaks how much of the digest matched.
    return hmac.compare_digest(digest, expected)


def needs_rehash(
    stored: str, *, n: int = DEFAULT_N, r: int = DEFAULT_R, p: int = DEFAULT_P
) -> bool:
    """Whether a stored hash was made with weaker parameters than the current ones.

    Asked after a successful verification, so a login on an old hash can re-hash it under the
    current parameters without a migration and without the staff member knowing.
    """
    parsed = _parse(stored)
    if parsed is None:
        return True
    stored_n, stored_r, stored_p, _, _ = parsed
    return (stored_n, stored_r, stored_p) != (n, r, p)


def generate_password(length: int = 20) -> str:
    """A random password, for the account an admin creates for a new hire.

    Drawn from a URL-safe alphabet so it can be pasted anywhere without escaping. Twenty
    characters of that alphabet is far past the twelve SEC-12 asks for, because this is a value
    nobody chose and nobody has to remember.
    """
    return secrets.token_urlsafe(length)


def _parse(stored: str) -> tuple[int, int, int, bytes, bytes] | None:
    """Take the algorithm, parameters, salt and digest out of a stored string.

    Returns None for anything that is not a scrypt string this module wrote, so every caller
    treats a corrupted or foreign value as a failed verification rather than an exception.
    """
    parts = stored.split("$")
    if len(parts) != 6 or parts[0] != _ALGORITHM:
        return None
    try:
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt = base64.b64decode(parts[4])
        digest = base64.b64decode(parts[5])
    except (ValueError, TypeError):
        return None
    if n < 2 or r < 1 or p < 1 or not salt or not digest:
        return None
    return n, r, p, salt, digest


def _maxmem(n: int, r: int, p: int) -> int:
    """The memory scrypt may use for these parameters, with headroom.

    ``hashlib.scrypt`` defaults to a 32 MiB ceiling, which is below what N=2**17 and r=8 need
    (roughly 128 * N * r bytes). The limit is set from the parameters themselves so a cost bump
    is not silently a crash.
    """
    return 256 * n * r * p + (32 * 1024 * 1024)
