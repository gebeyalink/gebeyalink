"""Proving an ingress callback came from the SMS gateway and not from anybody else.

The gateway signs the raw body with a shared secret and puts the digest in a header. This is
the one check that keeps ``POST /v1/sms/inbound`` from being a way for anyone who can reach
the port to post listings, place offers and answer tokens as whichever number they name. It
is therefore done before the body is parsed rather than after: a field parsed out of an
unsigned request is already a field somebody else chose.

The digest is over the *raw bytes* of the request. Re-encoding the parsed form fields would
change the spacing and ordering a form parser is free to normalise, and a signature check
that depended on a parser's rendering choices would be a check against this process rather
than against the sender. The caller reads ``request.body()`` and passes those bytes here.

Comparison is constant-time (``hmac.compare_digest``) so that the check cannot be split by
timing, and the provided value may carry the ``sha256=`` prefix some gateways use.
"""

from __future__ import annotations

import hashlib
import hmac


def sign_gateway_body(secret: str, body: bytes) -> str:
    """The signature this platform expects for a body, hex-encoded.

    Exists as its own function so a test can compute the signature a gateway would send
    without reaching into the verification path to do it.
    """
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def verify_gateway_signature(secret: str, body: bytes, provided: str | None) -> bool:
    """Whether ``provided`` is this secret's signature over these exact bytes.

    A missing header is a refusal, not an absent check: a gateway that sends no signature is
    exactly the case this exists for.
    """
    if not provided:
        return False
    candidate = provided.strip()
    if candidate.lower().startswith("sha256="):
        candidate = candidate[len("sha256=") :]
    return hmac.compare_digest(sign_gateway_body(secret, body), candidate.lower())
