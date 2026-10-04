"""NFR-07: a gap in the messages must break the build, not reach a trader.

A trader reads the platform in one of three languages, and nothing at runtime complains
when one language says less than another: the fallback to English keeps the request
answering, so a missing key or a forgotten placeholder is invisible until a trader reads
the gap and complains in a language nobody prepared. These tests hold the catalogues to
the rule — every key in every language, asking for the same values — and they hold the
checker itself to the standard this repository uses for response shapes: the broken case
is spelled out and proved to fail, not merely implied by the absence of a failure.

The broken catalogues below are the point of the file. A gate that has only ever seen
correct input has not been shown to fail, so each way of getting it wrong is written out
here and asserted to be reported.
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from packages.shared.i18n import (
    MESSAGES,
    SUPPORTED_LANGUAGES,
    Language,
    MessageCatalogError,
    assert_catalog_complete,
    find_catalog_problems,
    placeholders,
)

pytestmark = pytest.mark.unit

# Three keys in three languages: a greeting, a farewell, and a sentence with two
# placeholders. Deliberately broken variants of this catalogue are built below.
INTACT: dict[Language, dict[str, str]] = {
    Language.AM: {
        "farewell": "ደህና ሁን።",
        "greeting": "ሰላም።",
        "otp_message": "የGebeyaLink ኮድዎ {code} ነው። ለ{listing_code} ነው።",
    },
    Language.OM: {
        "farewell": "Hanga dhiraa.",
        "greeting": "Akeekkaa.",
        "otp_message": "Kooddii GebeyaLink {code} dha. {listing_code} fayyadamtu.",
    },
    Language.EN: {
        "farewell": "Goodbye.",
        "greeting": "Hello.",
        "otp_message": "Your GebeyaLink code is {code}. It is for {listing_code}.",
    },
}


def _without(language: Language, *keys: str) -> dict[Language, dict[str, str]]:
    """The intact catalogue with some keys quietly left out of one language."""
    catalog = {each: dict(messages) for each, messages in INTACT.items()}
    for key in keys:
        del catalog[language][key]
    return catalog


def _with(language: Language, messages: Mapping[str, str]) -> dict[Language, dict[str, str]]:
    """The intact catalogue with one language's sentences replaced."""
    catalog = {each: dict(kept) for each, kept in INTACT.items()}
    catalog[language] = dict(messages)
    return catalog


def _excluding(language: Language) -> dict[Language, dict[str, str]]:
    """The intact catalogue with one language never declared at all."""
    return {each: dict(messages) for each, messages in INTACT.items() if each is not language}


def test_NFR_07_the_shipped_catalogues_need_no_repair() -> None:
    """The gate passes on the messages the platform actually ships."""
    assert find_catalog_problems(MESSAGES) == []
    assert_catalog_complete(MESSAGES)


def test_NFR_07_the_shipped_text_message_asks_for_the_same_values_in_every_language() -> None:
    """The checker is not vacuous on real sentences: it reads the placeholders that ship.

    A gate that passes because it found nothing to read is a gate that would also pass on
    a catalogue that had lost its placeholders, so this pins what the shipped sentences
    actually contain.
    """
    asked = {
        language: placeholders(MESSAGES[language]["otp_message"])
        for language in SUPPORTED_LANGUAGES
    }

    assert asked[Language.EN] == {"code", "purpose"}
    assert len(set(asked.values())) == 1, f"placeholder sets differ between languages: {asked}"


@pytest.mark.parametrize(
    ("catalogue", "silent"),
    [
        (_without(Language.AM, "farewell"), "am"),
        (_without(Language.OM, "farewell"), "om"),
        (_without(Language.EN, "farewell"), "en"),
    ],
)
def test_NFR_07_a_key_left_out_of_one_language_fails_the_gate(
    catalogue: dict[Language, dict[str, str]],
    silent: str,
) -> None:
    """A sentence one language never learned is a build failure, naming key and language."""
    with pytest.raises(MessageCatalogError) as failure:
        assert_catalog_complete(catalogue)

    assert "farewell: missing from '" + silent + "'" in str(failure.value)


@pytest.mark.parametrize(
    ("catalogue", "absent"),
    [
        (
            _with(Language.EN, {**INTACT[Language.EN], "otp_hint": "Your code is {code}."}),
            ("am", "om"),
        ),
        (
            _with(Language.AM, {**INTACT[Language.AM], "otp_hint": "የኮድ ማስታወሻዎ {code}።"}),
            ("om", "en"),
        ),
        (_with(Language.OM, {**INTACT[Language.OM], "otp_hint": "Kooddii {code}."}), ("am", "en")),
    ],
)
def test_NFR_07_a_key_only_one_language_speaks_fails_the_gate(
    catalogue: dict[Language, dict[str, str]],
    absent: tuple[str, str],
) -> None:
    """A new sentence must reach every language, so the retired key cannot linger in one."""
    with pytest.raises(MessageCatalogError) as failure:
        assert_catalog_complete(catalogue)

    reported = str(failure.value)
    assert "2 message key problem(s)" in reported
    for language in absent:
        assert f"otp_hint: missing from '{language}'" in reported


def test_NFR_07_a_placeholder_a_translation_forgot_fails_the_gate() -> None:
    """The Amharic sentence dropped {listing_code}: renders, and silently loses the listing."""
    catalogue = _with(
        Language.AM,
        {**INTACT[Language.AM], "otp_message": "የGebeyaLink ኮድዎ {code} ነው።"},
    )

    with pytest.raises(MessageCatalogError) as failure:
        assert_catalog_complete(catalogue)

    reported = str(failure.value)
    assert "otp_message" in reported
    assert "listing_code" in reported
    assert "'am'" in reported and "'en'" in reported


def test_NFR_07_a_placeholder_only_one_translation_carries_fails_the_gate() -> None:
    """The Oromo sentence invented {purpose} the others do not ask for: no caller has it."""
    catalogue = _with(
        Language.OM,
        {**INTACT[Language.OM], "otp_message": "Kooddii {code} dha. {listing_code} {purpose}."},
    )

    with pytest.raises(MessageCatalogError) as failure:
        assert_catalog_complete(catalogue)

    assert "otp_message" in str(failure.value)
    assert "purpose" in str(failure.value)


def test_NFR_07_the_gate_names_every_problem_rather_than_the_first() -> None:
    """One run must report all of them, or fixing a catalogue is a guessing game."""
    catalogue = _without(Language.AM, "farewell")
    catalogue[Language.OM] = {
        **INTACT[Language.OM],
        "otp_message": "Kooddii GebeyaLink {code} dha.",
    }

    with pytest.raises(MessageCatalogError) as failure:
        assert_catalog_complete(catalogue)

    reported = str(failure.value)
    assert "2 message key problem(s)" in reported
    assert "farewell: missing from 'am'" in reported
    assert "otp_message" in reported and "listing_code" in reported


def test_NFR_07_a_language_with_no_messages_at_all_fails_the_gate() -> None:
    """A locale that is not declared is reported once, not once per key it is missing."""
    with pytest.raises(MessageCatalogError) as failure:
        assert_catalog_complete(_excluding(Language.OM))

    assert "1 message key problem(s)" in str(failure.value)
    assert "language 'om' has no messages at all" in str(failure.value)


def test_NFR_07_an_empty_catalogue_names_every_language_and_stops() -> None:
    """A catalogue with nothing in it is reported once per language, with no comparison left."""
    with pytest.raises(MessageCatalogError) as failure:
        assert_catalog_complete({})

    assert "3 message key problem(s)" in str(failure.value)
    for language in SUPPORTED_LANGUAGES:
        assert f"language '{language.value}' has no messages at all" in str(failure.value)


def test_NFR_07_placeholder_parity_is_still_judged_without_english() -> None:
    """If English is the language that vanished, the other two are measured against each other.

    English is the yardstick, so dropping it must not quietly switch placeholder checking
    off; it hands the job to the first language still declared.
    """
    catalogue = _excluding(Language.EN)
    catalogue[Language.OM] = {
        **INTACT[Language.OM],
        "otp_message": "Kooddii GebeyaLink {code} dha.",
    }

    with pytest.raises(MessageCatalogError) as failure:
        assert_catalog_complete(catalogue)

    reported = str(failure.value)
    assert "language 'en' has no messages at all" in reported
    assert "otp_message: 'om' asks for ['code']" in reported
    assert "but 'am' asks for ['code', 'listing_code']" in reported


def test_NFR_07_a_sentence_that_cannot_be_formatted_fails_the_gate() -> None:
    """An unclosed brace would raise inside a request; here it is a named build failure."""
    catalogue = _with(Language.OM, {**INTACT[Language.OM], "greeting": "Akeekkaa {"})

    with pytest.raises(MessageCatalogError) as failure:
        assert_catalog_complete(catalogue)

    assert "greeting: the 'om' sentence cannot be formatted" in str(failure.value)


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        ("Nothing to fill in.", frozenset()),
        ("Hello {name}.", frozenset({"name"})),
        ("{code} is for {listing_code}.", frozenset({"code", "listing_code"})),
        ("{code} and {code}.", frozenset({"code"})),
        ("{{literal}} braces.", frozenset()),
        ("{0} then {1}.", frozenset({"0", "1"})),
        ("{} then {}.", frozenset({"{}"})),
        ("{user.phone} and {user[0]}.", frozenset({"user"})),
    ],
)
def test_NFR_07_placeholders_are_read_out_of_the_sentence(
    template: str,
    expected: frozenset[str],
) -> None:
    """What the checker counts as "what this sentence asks for".

    Dotted and indexed access reduce to the name the caller passes, escaped braces are
    literal text, and two uses of one field is still one value to supply.
    """
    assert placeholders(template) == expected


def test_NFR_07_a_sentence_that_cannot_be_parsed_says_so() -> None:
    """The reader raises rather than guessing, and the gate turns that into a named failure."""
    with pytest.raises(ValueError):
        placeholders("unclosed {")
