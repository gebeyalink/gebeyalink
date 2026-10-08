"""The SMS command grammar of SPEC §14.4, as a pure function of the text.

This module is the whole of the reading half of SMS ingress: it turns a byte of text a
trader typed into a structured command, or into a refusal that says why. It touches no
database, no clock and no gateway, so the grammar can be asserted against directly rather
than through an endpoint.

Two decisions are worth stating rather than leaving to be inferred.

The result is a closed pair of types rather than a dictionary. A command either parses into
one of the nine verbs, carrying exactly the fields that verb has, or it does not parse, in
which case the caller is told the verb it thought it saw and the reason it was refused.
Nothing downstream has to guess whether a field is present.

Commands are case-insensitive and whitespace-normalised, as §14.4 requires: ``post`` and
``POST`` are the same word, and any run of spaces is one separator. The *arguments* keep
their case, because a crop name is a lookup key and a token is compared literally.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class CommandKind(StrEnum):
    """The nine verbs §14.4 defines, and nothing else."""

    HELP = "HELP"
    PRICE = "PRICE"
    POST = "POST"
    OFFER = "OFFER"
    YES = "YES"
    NO = "NO"
    CANCEL = "CANCEL"
    STATUS = "STATUS"
    PAYOUTS = "PAYOUTS"


class ParseFailure(StrEnum):
    """Why a piece of text did not parse into a command."""

    #: Empty text, or text that was only whitespace.
    EMPTY = "empty"
    #: The first word is not one of the nine verbs.
    UNKNOWN_VERB = "unknown_verb"
    #: The verb was recognised but was given the wrong number of arguments.
    WRONG_ARGUMENT_COUNT = "wrong_argument_count"
    #: A field that must be a number was not one.
    UNPARSEABLE_NUMBER = "unparseable_number"


@dataclass(frozen=True, slots=True)
class ParsedCommand:
    """One recognised command, with only the fields its verb actually carries.

    ``price_etb`` is held as the text the trader typed rather than as a number. The grammar
    allows a decimal point and the dispatcher is what decides how many decimal places money
    may have, so converting here would move a business rule into the lexer.
    """

    kind: CommandKind
    crop: str | None = None
    kg: int | None = None
    price_etb: str | None = None
    region: str | None = None
    token: str | None = None
    short_code: str | None = None
    order_code: str | None = None


@dataclass(frozen=True, slots=True)
class ParsedUnknown:
    """Text that is not a command, with the verb it offered and the reason it failed."""

    reason: ParseFailure
    verb: str | None = None


#: The verbs, keyed by the upper-case word a sender types.
_VERBS: Final[dict[str, CommandKind]] = {kind.value: kind for kind in CommandKind}

#: Whole kilograms as digits. The range is the service's to check; this only insists that
#: what arrived is a number rather than a word.
_KG_PATTERN: Final[re.Pattern[str]] = re.compile(r"[0-9]+")

#: A price in birr, with an optional decimal fraction. How many decimal places money may
#: carry is enforced where the value is turned into santim, not here.
_PRICE_PATTERN: Final[re.Pattern[str]] = re.compile(r"[0-9]+(?:\.[0-9]+)?")


def parse(text: str) -> ParsedCommand | ParsedUnknown:
    """Read one SMS body as a command, or explain why it is not one.

    Unknown text is not an exception: §14.4 says an unknown command is answered with the
    help text, which means "I did not understand you" is an ordinary outcome rather than a
    failure of the request.
    """
    tokens = text.split()
    if not tokens:
        return ParsedUnknown(ParseFailure.EMPTY)

    verb = tokens[0].upper()
    kind = _VERBS.get(verb)
    if kind is None:
        return ParsedUnknown(ParseFailure.UNKNOWN_VERB, verb=verb)

    args = tokens[1:]
    if kind is CommandKind.HELP or kind is CommandKind.PAYOUTS:
        if args:
            return ParsedUnknown(ParseFailure.WRONG_ARGUMENT_COUNT, verb=verb)
        return ParsedCommand(kind)

    if kind is CommandKind.PRICE:
        return _parse_price(verb, args)
    if kind is CommandKind.POST:
        return _parse_post(verb, args)
    if kind is CommandKind.OFFER:
        return _parse_offer(verb, args)
    if kind is CommandKind.YES or kind is CommandKind.NO:
        return _parse_token_command(kind, verb, args)
    # CANCEL and STATUS, both exactly one order code.
    if len(args) != 1:
        return ParsedUnknown(ParseFailure.WRONG_ARGUMENT_COUNT, verb=verb)
    return ParsedCommand(kind, order_code=args[0])


def _parse_price(verb: str, args: list[str]) -> ParsedCommand | ParsedUnknown:
    """``PRICE <crop>`` or ``PRICE <crop> <region>``."""
    if len(args) not in (1, 2):
        return ParsedUnknown(ParseFailure.WRONG_ARGUMENT_COUNT, verb=verb)
    region = args[1].upper() if len(args) == 2 else None
    return ParsedCommand(CommandKind.PRICE, crop=args[0], region=region)


def _parse_post(verb: str, args: list[str]) -> ParsedCommand | ParsedUnknown:
    """``POST <crop> <kg> <price_etb>`` — R10's worked example."""
    if len(args) != 3:
        return ParsedUnknown(ParseFailure.WRONG_ARGUMENT_COUNT, verb=verb)
    kg = _whole_number(args[1])
    if kg is None or not _PRICE_PATTERN.fullmatch(args[2]):
        return ParsedUnknown(ParseFailure.UNPARSEABLE_NUMBER, verb=verb)
    return ParsedCommand(CommandKind.POST, crop=args[0], kg=kg, price_etb=args[2])


def _parse_offer(verb: str, args: list[str]) -> ParsedCommand | ParsedUnknown:
    """``OFFER <short_code> <kg> <price_etb>``."""
    if len(args) != 3:
        return ParsedUnknown(ParseFailure.WRONG_ARGUMENT_COUNT, verb=verb)
    kg = _whole_number(args[1])
    if kg is None or not _PRICE_PATTERN.fullmatch(args[2]):
        return ParsedUnknown(ParseFailure.UNPARSEABLE_NUMBER, verb=verb)
    return ParsedCommand(CommandKind.OFFER, short_code=args[0], kg=kg, price_etb=args[2])


def _parse_token_command(
    kind: CommandKind, verb: str, args: list[str]
) -> ParsedCommand | ParsedUnknown:
    """``YES <token>`` / ``NO <token>``.

    The token's shape is deliberately not checked. Whether it is a token at all is answered
    by consuming it, and a token that does not resolve is the same sentence to the trader
    however malformed it was.
    """
    if len(args) != 1:
        return ParsedUnknown(ParseFailure.WRONG_ARGUMENT_COUNT, verb=verb)
    return ParsedCommand(kind, token=args[0])


def _whole_number(text: str) -> int | None:
    """A run of digits as an integer, or ``None`` if it is not one."""
    if not _KG_PATTERN.fullmatch(text):
        return None
    return int(text)
