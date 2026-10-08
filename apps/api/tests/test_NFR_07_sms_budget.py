"""NFR-07, SMS budgets: a rendered message that outgrows its language's SMS fails.

The budget is measured on the rendered string, not the template source. A placeholder
expands — a code becomes six characters, an order code nine — and the sentence is
counted after that expansion, because that is the string the gateway will actually carry.

Writing systems are decided by the language code, not by sniffing characters: en and om
are Latin, priced at up to 160 characters for one segment and 153 per segment once
concatenated; am and ti are Ge'ez, at up to 70 and 67. A variant that is absent is
skipped, not failed — ti is optional everywhere and simply never measured where it is
not written.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import pytest

from packages.shared.i18n.templates import MessageTemplate, load_templates

pytestmark = pytest.mark.unit

#: Placeholder fixtures the budget is measured against: six characters for a code, nine
#: for an order code, four for a quantity and a price.
PLACEHOLDER_FIXTURES: Final[dict[str, str]] = {
    "code": "123456",
    "order_code": "GL-123456",
    "quantity_kg": "1000",
    "crop": "Teff",
    "price": "2450",
}

#: What one SMS segment carries per writing system: Latin characters are GSM, Ge'ez is
#: 16-bit, so the Ge'ez budget is roughly half.
LATIN_SINGLE_SEGMENT_LIMIT: Final[int] = 160
LATIN_CONCATENATED_LIMIT: Final[int] = 153
GEEZ_SINGLE_SEGMENT_LIMIT: Final[int] = 70
GEEZ_CONCATENATED_LIMIT: Final[int] = 67

_LATIN_LANGUAGES: Final[frozenset[str]] = frozenset({"en", "om"})
_GEEZ_LANGUAGES: Final[frozenset[str]] = frozenset({"am", "ti"})


def single_segment_limit(language: str) -> int:
    """The longest rendered message, in characters, that fits one SMS segment."""
    if language in _LATIN_LANGUAGES:
        return LATIN_SINGLE_SEGMENT_LIMIT
    if language in _GEEZ_LANGUAGES:
        return GEEZ_SINGLE_SEGMENT_LIMIT
    raise AssertionError(f"no SMS budget is declared for language {language!r}")


def within_single_segment(rendered: str, language: str) -> bool:
    """Whether a rendered message fits its language's single-segment budget."""
    return len(rendered) <= single_segment_limit(language)


def _fixtures_for(template: MessageTemplate) -> dict[str, str]:
    """The fixture values for a template's declared placeholders."""
    unknown = sorted(set(template.placeholders) - set(PLACEHOLDER_FIXTURES))
    if unknown:
        raise AssertionError(f"no placeholder fixture declared for {template.id}: {unknown}")
    return {name: PLACEHOLDER_FIXTURES[name] for name in template.placeholders}


def test_every_shipped_template_fits_its_language_s_budget_when_rendered() -> None:
    """Every template, every language variant, rendered, fits that language's budget."""
    registry = load_templates()
    assert registry, "the template registry must not be empty"
    checked = 0
    for template in registry.values():
        for language, text in template.variants.items():
            rendered = text.format(**_fixtures_for(template))
            assert within_single_segment(rendered, language), (
                f"{template.id}/{language} renders to {len(rendered)} characters, over the "
                f"{single_segment_limit(language)}-character single-segment budget"
            )
            checked += 1
    assert checked >= 3 * len(registry), "every template must carry at least am, om and en"


def test_a_negative_latin_fixture_over_budget_fails_the_check() -> None:
    """The check fires on a 161-character message and passes at exactly 160."""
    assert within_single_segment("x" * LATIN_SINGLE_SEGMENT_LIMIT, "en") is True
    assert within_single_segment("x" * (LATIN_SINGLE_SEGMENT_LIMIT + 1), "en") is False


def test_a_negative_geez_fixture_over_budget_fails_the_check() -> None:
    """The check fires on a 71-character message and passes at exactly 70."""
    assert within_single_segment("አ" * GEEZ_SINGLE_SEGMENT_LIMIT, "am") is True
    assert within_single_segment("አ" * (GEEZ_SINGLE_SEGMENT_LIMIT + 1), "am") is False


def test_a_template_under_budget_in_source_fails_once_rendered() -> None:
    """The budget measures rendered length, so placeholder growth is the point."""
    source = "{c}" * 53  # 159 characters of template source: inside the Latin budget.
    assert len(source) <= LATIN_SINGLE_SEGMENT_LIMIT
    rendered: str = source.format(c="123456")
    assert within_single_segment(rendered, "en") is False


def test_concatenated_payload_limits_are_tighter_than_single_segment() -> None:
    """Multi-part messages pay for their headers: 153 Latin, 67 Ge'ez per segment."""
    assert LATIN_CONCATENATED_LIMIT < LATIN_SINGLE_SEGMENT_LIMIT
    assert GEEZ_CONCATENATED_LIMIT < GEEZ_SINGLE_SEGMENT_LIMIT
    assert len("x" * LATIN_CONCATENATED_LIMIT) <= LATIN_SINGLE_SEGMENT_LIMIT
    assert len("አ" * GEEZ_CONCATENATED_LIMIT) <= GEEZ_SINGLE_SEGMENT_LIMIT


def test_an_optional_geez_variant_is_measured_when_present_and_skipped_when_absent(
    tmp_path: Path,
) -> None:
    """ti is measured as Ge'ez when written; templates without it simply do not measure it."""
    (tmp_path / "extra.yaml").write_text(
        "- id: tigrinya_otp\n"
        "  channel: sms\n"
        "  priority: Q1\n"
        "  placeholders: [code]\n"
        "  variants:\n"
        '    am: "ኮድዎ {code}"\n'
        '    om: "Koodii kee {code}"\n'
        '    en: "Your code {code}"\n'
        '    ti: "ኮድካ {code}"\n',
        encoding="utf-8",
    )
    template = load_templates(tmp_path)["tigrinya_otp"]
    rendered = template.variants["ti"].format(code="123456")
    assert within_single_segment(rendered, "ti") is True
