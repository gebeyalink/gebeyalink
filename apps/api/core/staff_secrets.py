"""Keeping a staff member's TOTP secret unreadable at rest.

A TOTP secret is a bearer credential as potent as the password beside it: whoever holds it can
produce valid codes forever. It therefore does not sit in the column in the clear. It is
encrypted with Fernet — authenticated symmetric encryption from ``cryptography``, a dependency
the platform already carries — so a copy of the table is not a copy of every staff member's
second factor, and any tampering with a stored value is detected on the way out rather than
producing a wrong secret.

The key is a base64-encoded 32-byte value from configuration (``totp_encryption_key``). It is
required, with no default, for the same reason the signing keys are: a deployment that forgot
to inject it would otherwise encrypt with a value in the source tree, which is no encryption at
all.

Rotating this key is deliberately not implemented. Rotation means decrypting every stored secret
with the old key and writing it back under the new one, in one transaction, with the deployment
half-switched — a procedure worth designing rather than improvising, and one the ADR records as
deferred.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken


class SecretUnreadableError(Exception):
    """A stored secret could not be decrypted: wrong key, or the value was altered."""


class StaffSecretCipher:
    """Fernet encryption for the one secret a staff account keeps besides its password."""

    def __init__(self, key: str) -> None:
        # A malformed key is a deployment mistake, and Fernet's own error names it well enough
        # that wrapping it would only hide which key was wrong.
        self._fernet = Fernet(key.encode("ascii") if isinstance(key, str) else key)

    def encrypt(self, secret: bytes) -> str:
        """Encrypt a raw TOTP secret into the string the column holds."""
        return self._fernet.encrypt(secret).decode("ascii")

    def decrypt(self, token: str) -> bytes:
        """Recover a raw TOTP secret, refusing a token this key did not write.

        Raised rather than returned as None: an unreadable secret means the staff member cannot
        produce codes, which is a condition somebody has to be told about, not a boolean a
        caller silently treats as "wrong code".
        """
        try:
            return self._fernet.decrypt(token.encode("ascii"))
        except (InvalidToken, ValueError) as exc:
            raise SecretUnreadableError("stored TOTP secret could not be decrypted") from exc


def generate_key() -> str:
    """A fresh base64-encoded key, for the generation hint the ``.env.example`` documents."""
    return Fernet.generate_key().decode("ascii")
