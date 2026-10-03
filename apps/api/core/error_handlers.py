"""Turning failures into the one response shape clients are expected to parse.

A client should never have to guess whether an error came back as ``{"error": {...}}``,
a bare string, or a validation array from the request parser. Every failure leaving this
API arrives in the same envelope: a code from the shared catalog, a sentence in the
reader's language, machine-readable details, and the trace id to quote in a support
request.

Request-parser failures are re-shaped here too. They are the most common failure a client
will ever hit, and the default shape would be the one response it cannot handle.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from apps.api.core.trace import get_language, get_trace_id
from packages.shared.errors import ApiError, ErrorCode, http_status_for
from packages.shared.i18n.messages import translate

logger = logging.getLogger("gebeyalink.api")

#: Body returned when a request fails. Clients parse this shape and nothing else.
ERROR_ENVELOPE_KEY = "error"


def build_error_body(
    code: ErrorCode,
    message: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble the failure envelope."""
    return {
        ERROR_ENVELOPE_KEY: {
            "code": code.value,
            "message": message or translate(code.value.lower(), get_language()),
            "details": details or {},
            "trace_id": get_trace_id(),
        }
    }


def error_response(
    code: ErrorCode,
    status: int | None = None,
    message: str | None = None,
    details: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """Render a catalog failure as a response."""
    return JSONResponse(
        status_code=status or http_status_for(code),
        content=build_error_body(code, message=message, details=details),
        headers=headers,
    )


def _field_details(errors: Sequence[Any]) -> dict[str, Any]:
    """Reduce parser errors to what a client can act on: which field, and why."""
    fields: dict[str, str] = {}
    for error in errors:
        location = [str(part) for part in error.get("loc", ()) if part not in ("body", "query")]
        field = ".".join(location) or "request"
        fields[field] = str(error.get("msg", "invalid"))
    return {"fields": fields}


def register_error_handlers(app: FastAPI) -> None:
    """Install the handlers that guarantee a uniform failure shape."""

    @app.exception_handler(ApiError)
    async def _handle_api_error(_request: Request, exc: ApiError) -> JSONResponse:
        return error_response(exc.code, details=exc.details)

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return error_response(
            ErrorCode.VALIDATION_ERROR,
            message=translate("validation_error", get_language()),
            details=_field_details(exc.errors()),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(
        _request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        # A route raising "not found" should not leak FastAPI's bare {"detail": ...}
        # shape into a client that only knows how to read the envelope.
        code = ErrorCode.VALIDATION_ERROR if exc.status_code == 422 else ErrorCode.NOT_FOUND
        return error_response(code, status=exc.status_code)

    @app.exception_handler(Exception)
    async def _handle_unexpected(_request: Request, exc: Exception) -> JSONResponse:
        # Log the real cause with its trace id so an operator can find it, but answer with
        # nothing specific: an unexpected exception may describe data the caller must not see.
        logger.exception("unhandled failure trace_id=%s", get_trace_id(), exc_info=exc)
        return error_response(ErrorCode.INTERNAL_ERROR, status=500)
