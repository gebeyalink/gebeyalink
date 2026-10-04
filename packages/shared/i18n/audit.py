"""Does every language say everything the platform says?

A trader reads the platform in one of three languages, so a sentence that exists in only
one of them is a sentence most traders will never see. The runtime fallback to English
keeps the request alive, which is exactly why nothing would otherwise notice: the page
renders, the error arrives, and the gap stays invisible until somebody reads it and
complains in a language nobody prepared. Falling back is also a lie about what the
product speaks.

So parity is a build gate rather than a runtime behaviour. Two things must hold for every
key:

- the key exists in every language, and
- the placeholders one language's sentence names are exactly the placeholders every other
  language's sentence names for that key.

The placeholder rule matters as much as the key rule. An English sentence reading "your
listing {listing_code} was not found" beside an Amharic sentence that forgot
``{listing_code}`` both render. The second silently drops the listing the trader asked
about, and it only shows up when a trader reaches that code path in that language.

``find_catalog_problems`` returns the findings as lines somebody can act on.
``assert_catalog_complete`` turns them into an exception, which the test suite turns into
a red build.
"""

from __future__ import annotations

from collections.abc import Mapping
from string import Formatter
from typing import Final

from packages.shared.i18n.languages import SUPPORTED_LANGUAGES, Language

#: English is the sentence every other language ultimately falls back to, so it is the
#: yardstick a placeholder set is measured against.
REFERENCE_LANGUAGE: Final[Language] = Language.EN

#: How a positional field with no number written on it is spelled when reported. It means
#: "the next positional argument", which cannot be told apart from ``{0}`` without
#: counting the fields in the sentence.
_AUTO_POSITION: Final[str] = "{}"

_EXPECTED_RULE: Final[str] = (
    "Every key must exist in every language, asking for exactly the same placeholders."
)


class MessageCatalogError(RuntimeError):
    """The languages disagree about which keys exist, or about what those keys ask for."""


def placeholders(template: str) -> frozenset[str]:
    """The field names a sentence asks for, normalised so two languages can be compared.

    ``{code}`` is ``code`` and ``{0}`` is ``0``; ``{user.phone}`` and ``{user[0]}`` both
    reduce to ``user``, because that is the name the caller passes. Escaped braces are
    literal text and name nothing.

    A field nested inside a format specification (``{value:{width}}``) is not visited: the
    parser reads ``value`` and leaves the inner field alone. No sentence in the platform
    uses that form.

    Raises:
        ValueError: if the template cannot be formatted at all, which would raise inside a
            request instead of at build time.
    """
    names: set[str] = set()
    for _literal, field_name, _spec, _conversion in Formatter().parse(template):
        if field_name is None:
            continue
        base = field_name.split(".", maxsplit=1)[0].split("[", maxsplit=1)[0]
        names.add(base or _AUTO_POSITION)
    return frozenset(names)


def find_catalog_problems(
    catalog: Mapping[Language, Mapping[str, str]],
) -> list[str]:
    """Every disagreement between the languages, one actionable line each.

    A key counts as expected when any language speaks it, and is then required of them
    all. An absent language is reported on its own line and left out of the comparison,
    since every key would otherwise be reported missing from it as well.

    English is the yardstick for placeholders unless it is the language that is absent.
    """
    problems: list[str] = []
    present = [language for language in SUPPORTED_LANGUAGES if language in catalog]
    for language in SUPPORTED_LANGUAGES:
        if language not in catalog:
            problems.append(f"language {language.value!r} has no messages at all")
    if not present:
        # Every language is already named above and there is no sentence to compare, so
        # there is nothing further to say.
        return problems

    # English is the yardstick; if it is the language that is absent, the first language
    # still declared takes the job rather than placeholder checking switching off.
    reference = REFERENCE_LANGUAGE if REFERENCE_LANGUAGE in present else present[0]

    keys = sorted({key for language in present for key in catalog[language]})
    for key in keys:
        for language in present:
            if key not in catalog[language]:
                problems.append(f"{key}: missing from {language.value!r}")

        expected = _read(catalog[reference].get(key), key, reference, problems)
        if expected is None:
            continue
        for language in present:
            if language is reference or key not in catalog[language]:
                continue
            actual = _read(catalog[language][key], key, language, problems)
            if actual is None or actual == expected:
                continue
            problems.append(
                f"{key}: {language.value!r} asks for {sorted(actual)}, "
                f"but {reference.value!r} asks for {sorted(expected)}"
            )
    return problems


def assert_catalog_complete(catalog: Mapping[Language, Mapping[str, str]]) -> None:
    """Raise unless the languages agree, naming every disagreement rather than the first.

    Reporting only the first problem would make fixing a bad catalogue a guessing game:
    each run finds one more key, and the translator has to push to red again to see it.
    """
    problems = find_catalog_problems(catalog)
    if not problems:
        return
    listed = "\n".join(f"  - {problem}" for problem in problems)
    raise MessageCatalogError(
        f"{len(problems)} message key problem(s) across {len(catalog)} language(s).\n"
        f"{_EXPECTED_RULE}\n{listed}"
    )


def _read(
    template: str | None,
    key: str,
    language: Language,
    problems: list[str],
) -> frozenset[str] | None:
    """The placeholders in a sentence, or ``None`` once the sentence is reported as broken."""
    if template is None:
        return None
    try:
        return placeholders(template)
    except ValueError as error:
        problems.append(f"{key}: the {language.value!r} sentence cannot be formatted ({error})")
        return None
