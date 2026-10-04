"""What a trading business tells us about itself, and what we do with it.

The fields are optional throughout: a trader running a business may have a licence to show
and a tax number to declare, and one who has neither is still trading. What matters is that
a field that is given is kept, and that giving one changes nothing else about the account.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi.testclient import TestClient


def test_a_buyer_can_record_a_business_name_and_licence(
    client: TestClient, signed_in_as_buyer: Callable[..., dict[str, str]]
) -> None:
    """A trading business can be named, and the licence kept with it.

    These are the fields a reviewer reads when deciding whether to move somebody up a tier,
    so they have to be exactly what was sent.
    """
    headers = signed_in_as_buyer("+251911000007")
    response = client.patch(
        "/v1/me",
        headers=headers,
        json={"business_name": "Beko Export PLC", "license_no": "BL-2019-1180"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["business_name"] == "Beko Export PLC"
    assert body["license_no"] == "BL-2019-1180"


def test_a_tax_number_can_be_recorded_and_read_back(
    client: TestClient, signed_in_as_buyer: Callable[..., dict[str, str]]
) -> None:
    """A commercial tax identity is kept, and returned to its owner.

    Read back on a separate request so the test shows it was stored rather than echoed from
    the request that sent it.
    """
    headers = signed_in_as_buyer("+251911000007")
    client.patch("/v1/me", headers=headers, json={"tax_identification_number": "0010987654"})

    assert client.get("/v1/me", headers=headers).json()["tax_identification_number"] == "0010987654"


def test_recording_a_business_does_not_move_the_trader_up_a_tier(
    client: TestClient,
    signed_in_as_buyer: Callable[..., dict[str, str]],
) -> None:
    """Supplying the fields is not the same as being verified against them.

    A tier means a person or business has been checked, and no check happens here. Letting a
    write raise the tier would let anybody unlock larger transactions by typing a number.
    """
    headers = signed_in_as_buyer("+251911000007")
    response = client.patch(
        "/v1/me",
        headers=headers,
        json={
            "business_name": "Beko Export PLC",
            "license_no": "BL-2019-1180",
            "tax_identification_number": "0010987654",
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["kyc_tier"] == "TIER_0"


def test_business_fields_are_each_optional(
    client: TestClient,
    signed_in_as_buyer: Callable[..., dict[str, str]],
    profiles: Callable[[], list[dict[str, Any]]],
) -> None:
    """None of the three is required, and none of them is invented when left out.

    A trader who gives only a business name has no licence and no tax number, and the row
    says exactly that rather than carrying placeholders.
    """
    headers = signed_in_as_buyer("+251911000007")
    client.patch("/v1/me", headers=headers, json={"business_name": "Beko Export PLC"})

    stored = profiles()[0]
    assert stored["business_name"] == "Beko Export PLC"
    assert stored["license_no"] is None
    assert stored["tax_identification_number"] is None


def test_a_farmer_may_still_record_a_business_name(
    client: TestClient, signed_in: Callable[..., dict[str, str]]
) -> None:
    """The fields are not tied to a role.

    Somebody who grows a crop and sells it as a business is still a farmer on the platform,
    and refusing them a field they have a genuine answer for would be the wrong rule.
    """
    headers = signed_in("+251911000008")
    response = client.patch("/v1/me", headers=headers, json={"business_name": "Mamo's Coffee"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["business_name"] == "Mamo's Coffee"
    assert body["role"] == "FARMER"


def test_business_fields_can_be_changed_without_touching_the_rest(
    client: TestClient, signed_in_as_buyer: Callable[..., dict[str, str]]
) -> None:
    """Correcting one field leaves the others standing.

    A business that renames itself, or replaces a lapsed licence, should not have to restate
    everything else it told us.
    """
    headers = signed_in_as_buyer("+251911000007")
    client.patch(
        "/v1/me",
        headers=headers,
        json={
            "display_name": "Beko Chala",
            "business_name": "Beko Export PLC",
            "tax_identification_number": "0010987654",
        },
    )
    client.patch("/v1/me", headers=headers, json={"license_no": "BL-2024-9902"})

    body = client.get("/v1/me", headers=headers).json()
    assert body["license_no"] == "BL-2024-9902"
    assert body["business_name"] == "Beko Export PLC"
    assert body["tax_identification_number"] == "0010987654"
    assert body["display_name"] == "Beko Chala"


def test_a_long_business_name_is_kept_as_given(
    client: TestClient, signed_in_as_buyer: Callable[..., dict[str, str]]
) -> None:
    """Registered names run long, and the contract sets no length on this field.

    Nothing is silently trimmed or truncated on the way through, because a business name that
    comes back altered is worse than one that comes back exactly as typed.
    """
    headers = signed_in_as_buyer("+251911000007")
    name = "Ethiopian Highlands Fresh Fruit and Vegetable Export Enterprise PLC"

    client.patch("/v1/me", headers=headers, json={"business_name": name})

    assert client.get("/v1/me", headers=headers).json()["business_name"] == name
