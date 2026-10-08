"""FR-405, ``PRICE`` by text: the published average, in the unit the board publishes.

The price board quotes birr per quintal. The command asks by crop name, optionally overrides the
region the sender's profile carries, and answers from the *published* index rather than from
anything computed on the request. When there is nothing to quote - an unknown crop, or a region
with no average - the reply says so instead of guessing.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from fastapi.testclient import TestClient

from apps.api.providers.sms import RecordingSmsProvider

pytestmark = pytest.mark.integration

TRADER = "+251911000001"


def _signed_in_with_region(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    region: str,
) -> dict[str, str]:
    headers = signed_in(TRADER, language="en")
    updated = client.patch("/v1/me", headers=headers, json={"region_code": region, "woreda_id": 1})
    assert updated.status_code == 200, updated.text
    return headers


def test_price_quotes_the_published_average_for_the_senders_region(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    publish_price_index: Callable[..., None],
    sms_inbound: Callable[..., httpx.Response],
    sms_recorder: RecordingSmsProvider,
) -> None:
    _signed_in_with_region(client, signed_in, "OR")
    publish_price_index(crop_id=1, region_code="OR", average_santim=485_000)
    sms_inbound(client, "PRICE teff", from_phone=TRADER)
    assert sms_recorder.messages_to(TRADER)[-1].body == "Today Teff: 4,850 ETB/quintal"


def test_price_honours_a_region_argument_over_the_profile(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    publish_price_index: Callable[..., None],
    sms_inbound: Callable[..., httpx.Response],
    sms_recorder: RecordingSmsProvider,
) -> None:
    _signed_in_with_region(client, signed_in, "OR")
    publish_price_index(crop_id=1, region_code="OR", average_santim=485_000)
    publish_price_index(crop_id=1, region_code="AA", average_santim=999_900)
    sms_inbound(client, "PRICE teff AA", from_phone=TRADER)
    assert sms_recorder.messages_to(TRADER)[-1].body == "Today Teff: 9,999 ETB/quintal"


def test_price_names_a_crop_it_does_not_know(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    sms_inbound: Callable[..., httpx.Response],
    sms_recorder: RecordingSmsProvider,
) -> None:
    _signed_in_with_region(client, signed_in, "OR")
    sms_inbound(client, "PRICE zzzz", from_phone=TRADER)
    assert sms_recorder.messages_to(TRADER)[-1].body == "No price is available for zzzz."


def test_price_says_so_when_no_average_is_published(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    sms_inbound: Callable[..., httpx.Response],
    sms_recorder: RecordingSmsProvider,
) -> None:
    _signed_in_with_region(client, signed_in, "OR")
    sms_inbound(client, "PRICE teff", from_phone=TRADER)
    assert sms_recorder.messages_to(TRADER)[-1].body == "No price is available for teff."


def test_price_refuses_an_average_recorded_as_insufficient(
    client: TestClient,
    signed_in: Callable[..., dict[str, str]],
    publish_price_index: Callable[..., None],
    sms_inbound: Callable[..., httpx.Response],
    sms_recorder: RecordingSmsProvider,
) -> None:
    """A row that says "not enough sales" is not a price, however numeric it looks."""

    _signed_in_with_region(client, signed_in, "OR")
    publish_price_index(crop_id=1, region_code="OR", average_santim=485_000, insufficient_data=True)
    sms_inbound(client, "PRICE teff", from_phone=TRADER)
    assert sms_recorder.messages_to(TRADER)[-1].body == "No price is available for teff."
