"""FR-1706, ``PAYOUTS`` by text: the honest empty answer until payouts exist.

Payout records are a later slice. FR-1706 puts the command in the SMS grammar all the same, and
the only truthful reply today is that there is no history yet - not a menu, and not an invented
figure. This test holds the reply, rendered in the trader's own language, and holds that the
command itself writes nothing.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from fastapi.testclient import TestClient

from apps.api.providers.sms import RecordingSmsProvider

pytestmark = pytest.mark.integration

TRADER = "+251911000001"


def test_payouts_replies_with_an_empty_history(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    sms_inbound: Callable[..., httpx.Response],
    sms_recorder: RecordingSmsProvider,
) -> None:
    signed_in(TRADER, language="en")
    response = sms_inbound(client, "PAYOUTS", from_phone=TRADER)
    assert response.status_code == 200
    messages = sms_recorder.messages_to(TRADER)
    assert messages[-1].body == "You have no payouts yet (0)."


def test_payouts_does_not_write_an_order_or_a_listing(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    sms_inbound: Callable[..., httpx.Response],
    stored_orders: Callable[[], list[dict[str, object]]],
    stored_listings: Callable[[], list[dict[str, object]]],
) -> None:
    signed_in(TRADER, language="en")
    sms_inbound(client, "PAYOUTS", from_phone=TRADER)
    assert stored_orders() == []
    assert stored_listings() == []
