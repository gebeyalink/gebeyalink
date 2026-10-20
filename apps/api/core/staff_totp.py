"""One-time codes for staff sign-in, from the standard that defines them.

A staff member's second factor is a TOTP code: six digits that a shared secret and the clock
produce together, from RFC 6238. HMAC-SHA1 is the algorithm the standard's own examples use and
the one every authenticator app understands, so it is what this implements — nothing else. The
SHA256 and SHA512 variants the RFC also describes are outside this implementation's scope, and
the test that checks it against the RFC's vectors says so where it uses the SHA1 ones.

The window is ±1 step. A code is accepted if it matches the step the clock is in, the step
before, or the step after, which tolerates a minute of drift between the server and the handset
in either direction. That is the ordinary setting, and it is stated here rather than left to be
guessed at: a window of 1 means a code is usable for the better part of ninety seconds and no
longer.

The secret is raw bytes. A provisioning URI carries it base32-encoded because that is what the
``otpauth://`` scheme requires, and the encoder/decoder here convert between the two rather than
leaking base32 into the column: what is encrypted and stored is the raw secret, and base32 exists
only in the string a phone scans.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import struct
from datetime import UTC, datetime
from urllib.parse import quote, urlencode

#: How many seconds one code stands for, per RFC 6238 and every authenticator app.
TIME_STEP_SECONDS = 30

#: Six digits, the standard authenticator-app length. The RFC's own test vectors use eight and
#: are checked at eight in the test, which is why the digit count is a parameter and not a
#: constant baked into the arithmetic.
DEFAULT_DIGITS = 6

#: The accepted drift, in steps. One step each way is ±30 seconds.
DEFAULT_WINDOW = 1

#: Twenty random bytes, which encodes to the 32 base32 characters a provisioning URI carries.
SECRET_BYTES = 20


def generate_secret() -> bytes:
    """A fresh shared secret: 20 random bytes, the RFC's recommended length for SHA1."""
    return os.urandom(SECRET_BYTES)


def verify(
    code: str,
    secret: bytes,
    *,
    at: datetime | None = None,
    window: int = DEFAULT_WINDOW,
    digits: int = DEFAULT_DIGITS,
) -> bool:
    """Whether a code is valid for this secret around this moment.

    The comparison is constant-time per candidate. A wrong-length or non-numeric code is
    rejected before any comparison, because it cannot equal a generated code and there is no
    reason to hash for one.
    """
    if not code.isdigit() or len(code) != digits:
        return False
    moment = at or datetime.now(UTC)
    step = int(moment.timestamp()) // TIME_STEP_SECONDS
    for offset in range(-window, window + 1):
        if hmac.compare_digest(_at_step(secret, step + offset, digits), code):
            return True
    return False


def current_code(secret: bytes, *, at: datetime | None = None, digits: int = DEFAULT_DIGITS) -> str:
    """The code this secret produces for the current step.

    The mirror of ``verify``: a caller that holds the secret — the seed command printing a URI,
    or a test standing in for an authenticator app — needs to produce a code the server will
    accept, and this is that computation written once.
    """
    moment = at or datetime.now(UTC)
    step = int(moment.timestamp()) // TIME_STEP_SECONDS
    return _at_step(secret, step, digits)


def provisioning_uri(email: str, secret: bytes, *, issuer: str = "GebeyaLink") -> str:
    """The ``otpauth://`` URI a staff member scans into their authenticator.

    Returned exactly once, when the account is created, and never stored or served again: the
    URI carries the secret in a form a phone can read, and it is the operator's job to get it
    onto the staff member's device at that moment. Base32 because the scheme requires it.
    """
    label = quote(f"{issuer}:{email}", safe="")
    params = urlencode(
        {
            "secret": base64.b32encode(secret).decode("ascii").rstrip("="),
            "issuer": issuer,
            "algorithm": "SHA1",
            "digits": DEFAULT_DIGITS,
            "period": TIME_STEP_SECONDS,
        }
    )
    return f"otpauth://totp/{label}?{params}"


def _at_step(secret: bytes, step: int, digits: int) -> str:
    """The code this secret produces for one time step, per RFC 4226's HOTP.

    The counter is the time step, big-endian; the HMAC's low four bits pick the offset into the
    digest, and four bytes from there, masked, are taken modulo 10**digits.
    """
    counter = struct.pack(">Q", step)
    digest = hmac.new(secret, counter, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    truncated = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(truncated % (10**digits)).zfill(digits)
