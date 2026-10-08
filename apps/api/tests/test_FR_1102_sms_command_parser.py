"""FR-1102, the SMS command grammar: one text message becomes one typed command.

The grammar is a pure function, so it is asserted directly rather than through an endpoint. What
matters here is the boundary the specification draws: the nine verbs of §14.4, each with exactly
the arguments it names, and anything else refused with a reason rather than guessed at. The
worked example R10 - ``POST teff 2000 32.5`` - is the one case the specification spells out, and
it is asserted field by field.

These are unit tests: no database, no gateway, no clock. The text goes in and a value comes out.
"""

from __future__ import annotations

import pytest

from packages.shared.messaging.dispatcher import format_etb, parse_etb_to_santim
from packages.shared.messaging.inbound import (
    CommandKind,
    ParsedCommand,
    ParsedUnknown,
    ParseFailure,
    parse,
)

pytestmark = pytest.mark.unit


def test_help_parses_to_the_help_command() -> None:
    assert parse("HELP") == ParsedCommand(CommandKind.HELP)


def test_help_refuses_an_argument() -> None:
    """HELP takes nothing; a trailing word is a different intent than the one the verb names."""
    parsed = parse("HELP me")
    assert parsed == ParsedUnknown(ParseFailure.WRONG_ARGUMENT_COUNT, verb="HELP")


def test_price_takes_one_or_two_arguments() -> None:
    assert parse("PRICE teff") == ParsedCommand(CommandKind.PRICE, crop="teff")
    assert parse("PRICE teff OR") == ParsedCommand(CommandKind.PRICE, crop="teff", region="OR")


def test_price_refuses_a_third_argument() -> None:
    parsed = parse("PRICE teff OR extra")
    assert parsed == ParsedUnknown(ParseFailure.WRONG_ARGUMENT_COUNT, verb="PRICE")


def test_r10_worked_example_parses_field_by_field() -> None:
    """The specification's own example: ``POST teff 2000 32.5``."""
    assert parse("POST teff 2000 32.5") == ParsedCommand(
        CommandKind.POST, crop="teff", kg=2000, price_etb="32.5"
    )


def test_post_refuses_the_wrong_number_of_arguments() -> None:
    assert parse("POST teff 2000") == ParsedUnknown(ParseFailure.WRONG_ARGUMENT_COUNT, verb="POST")
    assert parse("POST teff 2000 32.5 now") == ParsedUnknown(
        ParseFailure.WRONG_ARGUMENT_COUNT, verb="POST"
    )


def test_post_refuses_a_weight_that_is_not_a_number() -> None:
    assert parse("POST teff two 32.5") == ParsedUnknown(
        ParseFailure.UNPARSEABLE_NUMBER, verb="POST"
    )


def test_post_refuses_a_price_that_is_not_a_number() -> None:
    assert parse("POST teff 2000 cheap") == ParsedUnknown(
        ParseFailure.UNPARSEABLE_NUMBER, verb="POST"
    )
    assert parse("POST teff 2000 32.5.1") == ParsedUnknown(
        ParseFailure.UNPARSEABLE_NUMBER, verb="POST"
    )


def test_offer_takes_a_code_a_weight_and_a_price() -> None:
    assert parse("OFFER GL-ABC123 100 30") == ParsedCommand(
        CommandKind.OFFER, short_code="GL-ABC123", kg=100, price_etb="30"
    )


def test_offer_refuses_a_third_field_it_does_not_name() -> None:
    assert parse("OFFER GL-ABC123 100 30 extra") == ParsedUnknown(
        ParseFailure.WRONG_ARGUMENT_COUNT, verb="OFFER"
    )


def test_yes_and_no_take_exactly_one_token() -> None:
    assert parse("YES A1B2C3D4") == ParsedCommand(CommandKind.YES, token="A1B2C3D4")
    assert parse("NO A1B2C3D4") == ParsedCommand(CommandKind.NO, token="A1B2C3D4")


def test_yes_refuses_two_tokens() -> None:
    assert parse("YES A1B2 C3D4") == ParsedUnknown(ParseFailure.WRONG_ARGUMENT_COUNT, verb="YES")


def test_a_token_is_not_checked_for_shape_here() -> None:
    """Whether a token works is answered by spending it, not by looking at it."""
    assert parse("YES notatoken") == ParsedCommand(CommandKind.YES, token="notatoken")


def test_cancel_and_status_take_one_order_code() -> None:
    assert parse("CANCEL GL-123456") == ParsedCommand(CommandKind.CANCEL, order_code="GL-123456")
    assert parse("STATUS GL-123456") == ParsedCommand(CommandKind.STATUS, order_code="GL-123456")


def test_cancel_refuses_a_missing_order_code() -> None:
    assert parse("CANCEL") == ParsedUnknown(ParseFailure.WRONG_ARGUMENT_COUNT, verb="CANCEL")


def test_payouts_takes_nothing() -> None:
    assert parse("PAYOUTS") == ParsedCommand(CommandKind.PAYOUTS)
    assert parse("PAYOUTS now") == ParsedUnknown(ParseFailure.WRONG_ARGUMENT_COUNT, verb="PAYOUTS")


def test_an_unknown_verb_is_refused_with_the_word_that_was_read() -> None:
    assert parse("SELL teff 100") == ParsedUnknown(ParseFailure.UNKNOWN_VERB, verb="SELL")


def test_empty_text_is_refused_as_empty() -> None:
    assert parse("") == ParsedUnknown(ParseFailure.EMPTY)
    assert parse("   ") == ParsedUnknown(ParseFailure.EMPTY)


def test_verbs_are_case_insensitive_and_arguments_keep_their_case() -> None:
    """§14.4 normalises the verb and the spacing, never the argument."""
    parsed = parse("  post   Teff   2000   32.5  ")
    assert parsed == ParsedCommand(CommandKind.POST, crop="Teff", kg=2000, price_etb="32.5")


def test_parse_etb_refuses_more_than_two_decimal_places() -> None:
    assert parse_etb_to_santim("32.5") == 3250
    assert parse_etb_to_santim("32") == 3200
    assert parse_etb_to_santim("32.50") == 3250
    assert parse_etb_to_santim("32.555") is None
    assert parse_etb_to_santim("32.") is None
    assert parse_etb_to_santim(".5") is None


def test_format_etb_drops_trailing_zeroes_nobody_typed() -> None:
    assert format_etb(3250) == "32.5"
    assert format_etb(3100) == "31"
    assert format_etb(3105) == "31.05"
