"""Sending text messages, behind an interface the rest of the platform never sees.

One implementation exists today: a backend that keeps messages in memory instead of
handing them to a network gateway. That is what development and tests run against, and it
is the reason the sign-in flow can be exercised end to end without buying messages or
holding gateway credentials in a repository.

The real gateway integration arrives with the merchant account and its credentials. Until
then the seam is deliberate rather than accidental: a service asks "send this message",
never "call this vendor", so swapping the backend touches this file and nothing above it.

The real adapter is declared here too — AfricasTalkingProvider — behind the messaging
interface the delivery slices define, and it refuses to send until the credentials it
needs resolve, so a message it is asked to carry is never silently dropped.

Messages carry verification codes, so nothing here logs a body. Diagnostics record that a
message was sent, to which number, and whether the backend accepted it — never its
contents.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol

from apps.api.core.config import Environment
from apps.api.core.hashing import generate_random_token
from packages.shared.messaging.provider import MessagingProvider, SendResult
from packages.shared.messaging.queues import SmsPriority

logger = logging.getLogger("gebeyalink.sms")


class ConfigurationError(RuntimeError):
    """A backend was requested that this deployment has not been set up with."""


@dataclass(frozen=True, slots=True)
class SmsMessage:
    """One message addressed to one phone number."""

    to: str
    body: str


@dataclass(frozen=True, slots=True)
class SmsReceipt:
    """The backend's answer: did it accept the message, and under what reference."""

    provider: str
    accepted: bool
    reference: str


class SmsProvider(Protocol):
    """What the platform needs from any messaging backend."""

    name: str

    async def send(self, message: SmsMessage) -> SmsReceipt:
        """Hand a message to the backend, or report that it could not be sent."""
        ...


@dataclass
class RecordingSmsProvider:
    """A backend that records messages instead of sending them.

    Deliberately not a mock: it is the honest behaviour of a deployment with no gateway
    configured. Whoever runs the API locally can read the verification codes out of it,
    and a test can assert exactly what a trader would have received.
    """

    name: str = "recording"
    sent: list[SmsMessage] = field(default_factory=list)

    async def send(self, message: SmsMessage) -> SmsReceipt:
        self.sent.append(message)
        # Logged without the body: the body carries a verification code, and a code in a
        # log file is a code handed to everyone who can read logs.
        logger.info("sms accepted by=%s to=%s", self.name, mask_phone(message.to))
        return SmsReceipt(provider=self.name, accepted=True, reference=generate_random_token(8))

    def messages_to(self, phone_e164: str) -> list[SmsMessage]:
        """Everything recorded for one number, oldest first."""
        return [message for message in self.sent if message.to == phone_e164]

    def clear(self) -> None:
        self.sent.clear()


@dataclass
class RecordingMessagingProvider:
    """The drain's recording backend, over the same store the sign-in flow records into.

    One store, one record: a rendered notification is handed to the same in-memory recorder
    the verification codes are, so whoever reads what a trader would have received reads it
    from one place rather than two. It is deliberately not a mock — it is the honest behaviour
    of a deployment with no gateway configured, and a test can assert exactly what went out.
    """

    recorder: RecordingSmsProvider
    name: str = "recording"

    async def send(
        self,
        phone_e164: str,
        body: str,
        *,
        priority: SmsPriority,
        template_id: str,
    ) -> SendResult:
        """Record a rendered message rather than hand it to a gateway.

        The lane and the template are the caller's classification and carry no decision here:
        a provider sends what it is given. The store keeps the message; the drain's own log
        carries the addressee and the lane it was queued under.
        """
        receipt = await self.recorder.send(SmsMessage(to=phone_e164, body=body))
        return SendResult(accepted=receipt.accepted, provider_ref=receipt.reference)


class AfricasTalkingProvider:
    """The real gateway's adapter, wired once the O-2 merchant credentials resolve.

    It deliberately has no behaviour yet: implementing the wire protocol before the
    credentials exist would mean inventing a sandbox to test it against, and an adapter
    that pretends to send is worse than one that refuses. Until then, ``send`` raises, so
    a message handed to it is never silently dropped, and the ``MessagingProvider``
    protocol it satisfies is the seam a later slice wires into the dispatcher.
    """

    async def send(
        self,
        phone_e164: str,
        body: str,
        *,
        priority: SmsPriority,
        template_id: str,
    ) -> SendResult:
        """Send a rendered message to the gateway, or refuse to pretend this happened."""
        raise NotImplementedError(
            "the Africa's Talking adapter is wired once the O-2 merchant credentials resolve"
        )


def mask_phone(phone_e164: str) -> str:
    """Keep the country code and the last two digits; lose the subscriber number.

    Enough to tell two messages apart in a log, not enough to become a list of customers.
    """
    if len(phone_e164) <= 5:
        return "***"
    return f"{phone_e164[:5]}***{phone_e164[-2:]}"


def build_sms_provider(name: str) -> SmsProvider:
    """Build the messaging backend this deployment is configured to use."""
    if name == "recording":
        return RecordingSmsProvider()
    raise ConfigurationError(
        f"No messaging backend named {name!r} is configured. "
        "The gateway integration ships with its merchant credentials."
    )


def build_messaging_provider(
    name: str, *, recorder: RecordingSmsProvider | None = None
) -> MessagingProvider:
    """Build the backend the notification drain hands rendered messages to.

    The same name configures both seams, and the recording adapter is built over the store it
    is given so a caller that already has one keeps one store. Absent a store, it is the
    process's own: a worker drains messages and reads no code, so nothing else in it shares.
    """
    if name == "recording":
        return RecordingMessagingProvider(recorder=recorder or RecordingSmsProvider())
    raise ConfigurationError(
        f"No messaging backend named {name!r} is configured. "
        "The gateway integration ships with its merchant credentials."
    )


def log_recording_provider_startup(name: str, environment: Environment) -> None:
    """Say, loudly enough for the deployment to hear, that nothing will leave the process.

    A recording backend is the honest answer to "no gateway is configured" in development and
    a trap in production: the process looks healthy and sends nothing. The same fact is a
    warning on one side of that line and a critical on the other, so a deployment that has
    left the fallback in place is loud rather than silent. Nothing behaves differently for it.
    """
    if name != "recording":
        return
    if environment is Environment.PRODUCTION:
        logger.critical(
            "recording SMS provider selected in production; no real SMS will be sent - "
            "set SMS_PROVIDER to a live provider"
        )
    else:
        logger.warning("recording SMS provider selected; no real SMS will be sent")
