"""The seam a rendered message crosses on its way to a network gateway.

A provider has no priority semantics of its own: it sends what it is given, the
classification and the quiet-hours decision having already happened upstream. It reports
whether the gateway took the message, under what reference, and why not if it did not —
the reason being the only context a dispatcher may surface back to an operator.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from packages.shared.messaging.queues import SmsPriority


@dataclass(frozen=True, slots=True)
class SendResult:
    """A provider's verdict on one message: taken or refused."""

    accepted: bool
    provider_ref: str | None = None
    reason: str | None = None


class MessagingProvider(Protocol):
    """Whatever hands a rendered message to a gateway."""

    async def send(
        self,
        phone_e164: str,
        body: str,
        *,
        priority: SmsPriority,
        template_id: str,
    ) -> SendResult:
        """Send ``body`` to ``phone_e164`` and report whether the gateway accepted it."""
        ...
