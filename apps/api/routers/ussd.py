"""The USSD gateway's inbound session endpoint, and nothing else.

A feature-phone dialled the short code and the network is now forwarding every keypress here
as a form of three fields — which session, which number, what has been typed so far — signed
with the same shared secret as the SMS gateway. This route proves the signature, lets the
session engine decide which menu is owed, and answers the call in the plain text the network
understands: ``CON`` when the menu is waiting for a keypress, ``END`` when the session is over.

The answer is a ``PlainTextResponse`` because the network speaks plain text, not JSON: it
reads the first three characters to decide whether to keep the call open or close it.
Everything else about the answer is decided by the engine and the registry, so this route
holds the same three checks the SMS route holds and no business rule.

    The signature is checked against the raw body before anything is parsed. A field read out
    of an unsigned request is a field somebody else chose, and there would be no point
    checking afterwards. The one secret both gateways share is why a bad signature is the same
    401 here as there.

    A malformed request is the gateway's fault and is a 422 with the standard error envelope:
    a session id or number is missing, or the body is not urlencoded UTF-8. Neither reaches
    the engine, which — like the SMS dispatcher — is answered with the reply, not with the
    state of the session.

    An empty ``text`` is not malformed. It is the first press of a dial: the trader has just
    reached the top of a new session, and the engine's answer is the first menu.
"""

from __future__ import annotations

import logging
from urllib.parse import parse_qsl

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

from apps.api.dependencies import (
    UssdGatewayDep,
    UssdSessionStoreDep,
    ussd_engine_dep,
)
from packages.shared.errors import ApiError, ErrorCode
from packages.shared.messaging.signature import verify_gateway_signature
from packages.ussd.engine import handle

logger = logging.getLogger("gebeyalink.api")

router = APIRouter(tags=["Messaging"])

#: The header the gateway signs with, the same one the SMS inbound route reads.
SIGNATURE_HEADER: str = "X-AT-Signature"


@router.post("/ussd/session", status_code=200)
async def ussd_session(
    request: Request,
    gateway: UssdGatewayDep,
    store: UssdSessionStoreDep,
    engine: ussd_engine_dep,
) -> PlainTextResponse:
    """Answer one keypress with the plain-text screen the network should show."""
    settings = request.app.state.settings
    raw = await request.body()
    secret = settings.gateway_secret.get_secret_value()
    provided = request.headers.get(SIGNATURE_HEADER)
    if not verify_gateway_signature(secret, raw, provided):
        # The body is not logged: it is the material an attacker would want reflected, and
        # the length tells a truncated callback from a forged one.
        logger.warning("ussd inbound signature refused length=%s", len(raw))
        raise ApiError(ErrorCode.GATEWAY_SIGNATURE_INVALID)

    fields = _form_fields(raw)
    missing = [name for name in ("sessionId", "phoneNumber") if not fields.get(name)]
    if missing:
        absent = dict.fromkeys(missing, "required")
        raise ApiError(ErrorCode.VALIDATION_ERROR, details={"fields": absent})

    body = await handle(
        store=store,
        gateway=gateway,
        nodes=engine,
        session_id=fields["sessionId"],
        phone_e164=fields["phoneNumber"],
        text=fields.get("text", ""),
    )
    logger.info("ussd session answered bytes=%s", len(body))
    return PlainTextResponse(content=body, media_type="text/plain")


def _form_fields(raw: bytes) -> dict[str, str]:
    """Parse the urlencoded body, or refuse it as malformed.

    Parsed manually rather than by the framework because the body has already been read for
    the signature check and cannot be read twice.
    """
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        logger.warning("ussd inbound body not utf-8 length=%s reason=%s", len(raw), error.reason)
        raise ApiError(
            ErrorCode.VALIDATION_ERROR, details={"fields": {"body": "not_urlencoded_utf8"}}
        ) from None
    return dict(parse_qsl(decoded, keep_blank_values=True))
