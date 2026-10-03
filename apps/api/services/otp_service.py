"""Issuing and checking verification codes.

A code is the only thing standing between a stranger and somebody's account, so the flow
around it is deliberately unfriendly: it is short, it expires on its own, it tolerates a
fixed number of wrong guesses, it cannot be re-sent in a hurry, and there is a daily
ceiling on how many one number may receive at all.

One rule governs everything below: the readable code exists only for the moment it is put
into a text message. From then on it is a digest and a salt, and no log line, error message,
or database row ever holds the digits.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from apps.api.core.config import Settings
from apps.api.core.hashing import digest, digests_match, generate_salt
from apps.api.providers.sms import SmsMessage, SmsProvider
from apps.api.repositories.verification_code_repository import VerificationCodeRepository
from packages.shared.errors import ApiError, ErrorCode
from packages.shared.i18n import Language
from packages.shared.i18n.messages import translate

#: What a code being sent is for, so the trader can tell why they received one.
CODE_PURPOSE_SIGN_IN = "sign_in"


@dataclass(frozen=True, slots=True)
class CodeDispatch:
    """What the caller learns after a code goes out.

    Carries the lifetime and the wait before another may be sent, so a client can explain
    itself without hardcoding the numbers.
    """

    phone_e164: str
    expires_in_seconds: int
    resend_after_seconds: int


class OtpService:
    """Owns the verification-code lifecycle."""

    def __init__(
        self,
        codes: VerificationCodeRepository,
        sms: SmsProvider,
        settings: Settings,
    ) -> None:
        self._codes = codes
        self._sms = sms
        self._settings = settings

    def _generate_code(self) -> str:
        """A code of exactly the configured length, digits only.

        Drawn from the system entropy source rather than a random number generator: a
        predictable code is the one failure this whole class exists to prevent.
        """
        upper_bound = 10**self._settings.otp_code_digits
        return f"{secrets.randbelow(upper_bound):0{self._settings.otp_code_digits}d}"

    async def send_code(
        self,
        phone_e164: str,
        language: Language,
        purpose: str = CODE_PURPOSE_SIGN_IN,
    ) -> CodeDispatch:
        """Put a fresh code in front of its owner."""
        code = self._generate_code()
        salt = generate_salt()
        await self._codes.store(
            phone_e164=phone_e164,
            digest=digest(code, salt),
            salt=salt,
            ttl_seconds=self._settings.otp_ttl_seconds,
        )
        await self._codes.start_cooldown(phone_e164, self._settings.otp_resend_cooldown_seconds)

        purpose_text = translate(f"otp_message_purpose_{purpose}", language)
        receipt = await self._sms.send(
            SmsMessage(
                to=phone_e164,
                body=translate("otp_message", language, code=code, purpose=purpose_text),
            )
        )
        if not receipt.accepted:
            # Nothing was delivered, so leave no pending code behind: a code the trader
            # never received would otherwise consume one of their guesses when they try it.
            await self._codes.spend(phone_e164)
            raise ApiError(
                ErrorCode.VALIDATION_ERROR,
                details={"delivery": "rejected"},
            )

        return CodeDispatch(
            phone_e164=phone_e164,
            expires_in_seconds=self._settings.otp_ttl_seconds,
            resend_after_seconds=self._settings.otp_resend_cooldown_seconds,
        )

    async def check_code(self, phone_e164: str, code: str) -> None:
        """Accept the code and spend it, or say precisely why it was refused.

        A correct code is spent on use. That is what makes a single-use code single-use
        even if it is observed in transit after a legitimate sign-in.

        The guess budget is checked before the code is even looked at. A number that has
        already made five wrong guesses is refused on its next attempt whatever it submits,
        because a sixth attempt is past the limit rather than a lucky one — otherwise the
        one code an attacker really wants is the one they may still try.
        """
        budget = self._settings.otp_max_attempts

        if await self._codes.is_locked(phone_e164):
            raise ApiError(
                ErrorCode.OTP_LOCKED,
                details={"max_attempts": budget, "reason": "guesses_spent"},
            )

        attempts_used = await self._codes.attempts_used(phone_e164)
        if attempts_used >= budget:
            # This attempt is one past the limit. The code goes with it: it is now known to
            # somebody who has failed to guess it, so leaving it in place would only make
            # the next guess worth making.
            await self._codes.lock(phone_e164, self._settings.otp_ttl_seconds)
            raise ApiError(
                ErrorCode.OTP_LOCKED,
                details={"max_attempts": budget, "attempts_used": attempts_used},
            )

        stored = await self._codes.load(phone_e164)
        if stored is None:
            raise ApiError(
                ErrorCode.OTP_EXPIRED,
                details={"reason": "no_code_pending"},
            )

        if digests_match(digest(code, stored.salt), stored.digest):
            await self._codes.spend(phone_e164)
            return

        guess = await self._codes.register_failed_guess(phone_e164, budget)
        raise ApiError(
            ErrorCode.OTP_EXPIRED,
            details={
                "reason": "incorrect_code",
                "attempts_remaining": guess.attempts_remaining,
            },
        )

    async def resend_wait_remaining(self, phone_e164: str) -> int:
        """Seconds before this number may be sent another code."""
        return await self._codes.cooldown_remaining(phone_e164)
