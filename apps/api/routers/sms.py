"""The SMS gateway's inbound webhook, and nothing else.

A gateway posts a form-encoded body of two fields — who it is from and what it says — signed
with a shared secret over the raw bytes. This route proves the signature, reads one command,
lets the dispatcher decide what the command means, renders the reply the registry holds for
it, and hands that reply to the messaging backend. The gateway is answered with an empty 200
whatever the command did, because a gateway that retries on a business refusal would retry a
POST that was already refused for a reason retrying cannot change.

Three decisions are worth stating rather than leaving to be inferred.

    The signature is checked against the raw body before anything is parsed from it. A field
    read out of an unsigned request is a field somebody else chose, and there would be no
    point checking afterwards.

    A malformed request is the gateway's fault and is a 422 with the standard error envelope;
    a bad or missing signature is a 401. Neither reaches the dispatcher.

    The reply is built here and nowhere else. The dispatcher names a template and the values
    its placeholders take; this route is the only place that turns that into characters and a
    language, so the shared package never has to know how a message is rendered.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Final
from urllib.parse import parse_qsl

from fastapi import APIRouter, Request, Response

from apps.api.core.config import Settings
from apps.api.dependencies import SmsCommandGatewayDep, sms_dep
from apps.api.providers.sms import SmsMessage
from packages.shared.errors import ApiError, ErrorCode
from packages.shared.i18n.languages import DEFAULT_LANGUAGE, Language
from packages.shared.i18n.templates import load_templates
from packages.shared.messaging.dispatcher import dispatch
from packages.shared.messaging.inbound import parse
from packages.shared.messaging.queues import priority_for
from packages.shared.messaging.signature import verify_gateway_signature

logger = logging.getLogger("gebeyalink.api")

router = APIRouter(tags=["Messaging"])

#: The longest body this route will look at. A real command is a line of text; a body larger
#: than this is not one, and refusing it before the parser keeps a hostile sender from making
#: the parser walk a megabyte of words.
MAX_TEXT_LENGTH: Final[int] = 1600

#: The header the gateway signs with, per docs/openapi.yaml.
SIGNATURE_HEADER: Final[str] = "X-AT-Signature"


@router.post("/sms/inbound", status_code=200)
async def inbound_sms(
    request: Request,
    gateway: SmsCommandGatewayDep,
    sms: sms_dep,
) -> Response:
    """Take one signed text message, act on it, and reply by text.

    Always answers 200 to a signed, well-formed request, whether the command succeeded or was
    refused for a business reason. The reply the trader receives is the result; the gateway is
    told only that the message was received.
    """
    settings: Settings = request.app.state.settings
    raw = await request.body()
    secret = settings.gateway_secret.get_secret_value()
    provided = request.headers.get(SIGNATURE_HEADER)
    if not verify_gateway_signature(secret, raw, provided):
        # The body is not logged: it is precisely the material an attacker would want to see
        # reflected, and the length is enough to tell a truncated callback from a forged one.
        logger.warning("sms inbound signature refused length=%s", len(raw))
        raise ApiError(ErrorCode.GATEWAY_SIGNATURE_INVALID)

    fields = _form_fields(raw)
    missing = [name for name in ("from", "text") if name not in fields]
    if missing or not fields["from"]:
        absent = dict.fromkeys(missing, "required")
        if not missing and not fields["from"]:
            absent["from"] = "required"
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            details={"fields": absent},
        )
    sender = fields["from"]
    # An empty body of text is not a malformed request; it is a message with no command in
    # it, and the parser answers that the same way it answers any word it does not know.
    text = fields["text"]
    if len(text) > MAX_TEXT_LENGTH:
        raise ApiError(ErrorCode.VALIDATION_ERROR, details={"fields": {"text": "too_long"}})

    command = parse(text)
    trader = await gateway.resolve_trader(sender)
    key = f"sms:{sender}:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"
    template_id, context = await dispatch(command, trader, gateway, key)

    language = trader.language if trader is not None else DEFAULT_LANGUAGE
    body = _render(template_id, language, context)
    await sms.send(SmsMessage(to=sender, body=body))
    logger.info(
        "sms command handled template=%s priority=%s",
        template_id,
        priority_for(template_id).value,
    )
    return Response(status_code=200)


def _form_fields(raw: bytes) -> dict[str, str]:
    """Parse the urlencoded body, or refuse it as malformed.

    Parsed manually rather than by the framework because the body has already been read for
    the signature check and cannot be read twice. A body that is not UTF-8 urlencoded text is
    the gateway's mistake, not a command, so it is a validation failure.
    """
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        logger.warning("sms inbound body not utf-8 length=%s reason=%s", len(raw), error.reason)
        raise ApiError(
            ErrorCode.VALIDATION_ERROR, details={"fields": {"body": "not_urlencoded_utf8"}}
        ) from None
    return dict(parse_qsl(decoded, keep_blank_values=True))


def _render(template_id: str, language: Language, context: dict[str, str]) -> str:
    """Turn a template id and its context into the characters a trader reads.

    The registry is the one place a message's wording lives, so the dispatcher never returns
    a string and this function never invents one. Rendering happens in the trader's own
    language; a missing translation falls back the way the registry already falls back.
    """
    template = load_templates()[template_id]
    variant = template.variants.get(language.value) or template.variants["en"]
    return variant.format(**context)
