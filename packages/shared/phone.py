"""Phone-number rules for the Ethiopian mobile network.

Registration and sign-in both start from a phone number, and it is the only identifier a
trader has before they have an account. It therefore has to be checked the same way
everywhere — in the app, over USSD, and in the admin console — which is why the rule lives
in the shared package instead of inside one service.

Only Ethiopian mobile numbers are accepted: nine-digit subscriber numbers beginning 9
(Safaricom, Ethio Telecom) or 7 (Ethio Telecom), behind the +251 country code.
"""

from __future__ import annotations

import re

#: Nine digits after the country code, beginning 7 or 9. Landlines and length prefixes
#: are rejected rather than trimmed: a number we have to guess at is a number we cannot
#: send a verification code to with confidence.
ETHIOPIAN_MOBILE_PATTERN = re.compile(r"^\+251(9|7)\d{8}$")

COUNTRY_CODE = "+251"


def is_valid_ethiopian_mobile(phone: str) -> bool:
    """Whether the number can receive a verification message."""
    return ETHIOPIAN_MOBILE_PATTERN.fullmatch(phone) is not None
