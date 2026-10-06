"""The platform's error vocabulary.

Services do not build HTTP responses. They raise :class:`ApiError` carrying a code from
the shared catalog; the web layer converts that into a status, a sentence in the
caller's language, and a trace id. Keeping the vocabulary in one package means the
mobile app, the USSD gateway, and the admin console can recognise the same failures
without parsing prose.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

_CATALOG_PATH = Path(__file__).with_name("errors.yaml")


class ErrorCode(StrEnum):
    """Every failure the platform can report.

    Mirrors the catalog file next to this module. A test asserts the two stay identical,
    so adding a code in one place without the other breaks the build rather than shipping.
    """

    INTERNAL_ERROR = "INTERNAL_ERROR"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    RATE_LIMITED = "RATE_LIMITED"
    OTP_LOCKED = "OTP_LOCKED"
    OTP_EXPIRED = "OTP_EXPIRED"
    TOKEN_INVALID = "TOKEN_INVALID"
    CONCURRENT_MODIFICATION = "CONCURRENT_MODIFICATION"
    ILLEGAL_STATE_TRANSITION = "ILLEGAL_STATE_TRANSITION"
    OFFER_LIMIT = "OFFER_LIMIT"
    OFFER_CONFLICT = "OFFER_CONFLICT"
    ORDER_CONFLICT = "ORDER_CONFLICT"
    OFFER_EXPIRED = "OFFER_EXPIRED"
    LIMIT_EXCEEDED = "LIMIT_EXCEEDED"
    INSUFFICIENT_KYC = "INSUFFICIENT_KYC"
    PAYMENT_FAILED = "PAYMENT_FAILED"
    PAYMENT_EXPIRED = "PAYMENT_EXPIRED"
    DISPUTE_EXISTS = "DISPUTE_EXISTS"
    KYC_PENDING = "KYC_PENDING"
    LISTING_NOT_ACTIVE = "LISTING_NOT_ACTIVE"
    GATEWAY_SIGNATURE_INVALID = "GATEWAY_SIGNATURE_INVALID"
    PAYMENT_ATTEMPTS_EXCEEDED = "PAYMENT_ATTEMPTS_EXCEEDED"
    PRICE_OUTLIER_INFO = "PRICE_OUTLIER_INFO"


@dataclass(frozen=True, slots=True)
class ErrorDefinition:
    """How one catalog entry behaves at the edge of the system."""

    code: ErrorCode
    status: int
    raises: bool
    message_key: str


class ApiError(Exception):
    """A failure a client is allowed to see, described in platform terms.

    ``details`` carries the machine-readable specifics a client needs to recover, such as
    how many attempts remain or when a limit resets. Anything a caller should not learn
    (whether a phone exists, which password matched) stays out of it.

    Deliberately a plain exception rather than a dataclass: a dataclass makes the instance
    immutable, and an exception that cannot carry its own traceback cannot be traced,
    caught, or re-raised properly.
    """

    def __init__(self, code: ErrorCode, details: dict[str, Any] | None = None) -> None:
        super().__init__(code.value)
        self.code = code
        self.details: dict[str, Any] = details if details is not None else {}

    @property
    def definition(self) -> ErrorDefinition:
        return error_definition(self.code)

    def __repr__(self) -> str:
        return f"ApiError(code={self.code.value!r}, details={self.details!r})"


def _load_catalog() -> dict[ErrorCode, ErrorDefinition]:
    raw = yaml.safe_load(_CATALOG_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise RuntimeError(f"{_CATALOG_PATH.name} must contain a mapping of code to definition")

    catalog: dict[ErrorCode, ErrorDefinition] = {}
    for name, body in raw.items():
        code = ErrorCode(name)
        catalog[code] = ErrorDefinition(
            code=code,
            status=int(body["status"]),
            raises=bool(body["raises"]),
            message_key=str(body["message"]),
        )
    return catalog


_CATALOG = _load_catalog()


def error_definition(code: ErrorCode) -> ErrorDefinition:
    try:
        return _CATALOG[code]
    except KeyError as exc:  # pragma: no cover - guards a catalog/enum mismatch
        raise RuntimeError(f"{code.value} is missing from {_CATALOG_PATH.name}") from exc


def http_status_for(code: ErrorCode) -> int:
    """The status a failure is reported with."""
    return error_definition(code).status
