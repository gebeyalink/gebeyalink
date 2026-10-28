"""NFR-07 — every message declares who wrote its words.

The registry carries human-language text for am and om and en that the SPEC does not
supply verbatim. Only one SMS template in the whole specification is spelled out
word-for-word — the Amharic and English of ``offer_received_buyer_sms`` in SPEC §14.5 —
so most of what this registry holds is this team's authored copy. That copy is a trust
risk in a trilingual-first product until a native speaker has read it, which is exactly
what open item O-3 exists to prevent.

Neither the budget gate nor the lane gate can tell "the specification's words" apart
from "our words": one measures a string, the other classifies a string. This test pins
the field that makes the difference visible. It does not require any template to be
spec-supplied; it requires every template to say honestly which one it is, and refuses a
value that is neither.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path

import pytest

from apps.api.management import export_translations
from apps.api.tests.test_NFR_07_sms_budget import PLACEHOLDER_FIXTURES as BUDGET_FIXTURES
from packages.shared.i18n.templates import TemplateReview, hash_variant, load_templates

pytestmark = pytest.mark.unit

EXPECTED_COLUMNS = (
    "template_id",
    "channel",
    "priority",
    "language",
    "rendered_copy",
    "placeholders",
    "rendered_length",
    "budget",
    "within_budget",
    "review_status_current",
    "reviewer",
    "reviewed_at",
    "decision",
    "revised_text",
    "notes",
)


def test_every_template_declares_one_of_the_two_review_statuses() -> None:
    """Nothing ships without a provenance label, and no label outside the two is accepted."""
    registry = load_templates()
    assert registry, "the template registry must not be empty"
    for template_id, template in registry.items():
        assert template.review_status in (
            TemplateReview.SPEC_SUPPLIED,
            TemplateReview.UNREVIEWED,
        ), (
            f"{template_id} declares review_status {template.review_status!r}, "
            "which is neither spec_supplied nor unreviewed"
        )


def test_every_variant_carries_its_own_provenance_label() -> None:
    """The map is complete: each language has a label, and it is one of the two real ones.

    A template's single ``review_status`` is not enough when the specification supplies one
    language and leaves another to this team. The per-variant map is what keeps the Oromo of
    a menu that came from the SPEC from borrowing the SPEC's authority.
    """
    registry = load_templates()
    assert registry, "the template registry must not be empty"
    for template_id, template in registry.items():
        assert set(template.variant_review_status) == set(template.variants), (
            f"{template_id} labels {sorted(template.variant_review_status)} but carries "
            f"{sorted(template.variants)}"
        )
        for language, status in template.variant_review_status.items():
            assert status in (
                TemplateReview.SPEC_SUPPLIED,
                TemplateReview.UNREVIEWED,
            ), f"{template_id}/{language} declares {status!r}, which is not provenance"


def test_a_partial_variant_review_status_map_is_refused_and_named(tmp_path: Path) -> None:
    """A map that leaves a language unlabelled is refused, and the missing language is named."""
    (tmp_path / "registry.yaml").write_text(
        "- id: ussd_main_menu\n"
        "  channel: ussd\n"
        "  priority: Q2\n"
        "  review_status: spec_supplied\n"
        "  variant_review_status:\n"
        "    am: spec_supplied\n"
        "    en: spec_supplied\n"
        "  placeholders: []\n"
        "  variants:\n"
        '    am: "አንድ"\n'
        '    om: "Tokko"\n'
        '    en: "One"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError) as failure:
        load_templates(tmp_path)
    message = str(failure.value)
    assert "ussd_main_menu" in message
    assert "om" in message


def test_a_variant_label_the_registry_does_not_know_is_refused_and_named(
    tmp_path: Path,
) -> None:
    """A per-variant label outside the two is refused, with the template and value named."""
    (tmp_path / "registry.yaml").write_text(
        "- id: ussd_main_menu\n"
        "  channel: ussd\n"
        "  priority: Q2\n"
        "  review_status: unreviewed\n"
        "  variant_review_status:\n"
        "    am: approved\n"
        "    om: unreviewed\n"
        "    en: unreviewed\n"
        "  placeholders: []\n"
        "  variants:\n"
        '    am: "አንድ"\n'
        '    om: "Tokko"\n'
        '    en: "One"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError) as failure:
        load_templates(tmp_path)
    message = str(failure.value)
    assert "ussd_main_menu" in message
    assert "approved" in message


def test_a_review_status_the_registry_does_not_know_is_refused_and_named(
    tmp_path: Path,
) -> None:
    """A working-flow word like 'approved' is not provenance, and is refused by name."""
    (tmp_path / "registry.yaml").write_text(
        "- id: auth_otp\n"
        "  channel: sms\n"
        "  priority: Q1\n"
        "  review_status: approved\n"
        "  placeholders: [code]\n"
        "  variants:\n"
        '    am: "ኮድዎ {code}"\n'
        '    om: "Koodii kee {code}"\n'
        '    en: "Your code {code}"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError) as failure:
        load_templates(tmp_path)
    message = str(failure.value)
    assert "auth_otp" in message
    assert "approved" in message


def test_a_template_with_no_review_status_is_refused_and_named(tmp_path: Path) -> None:
    """The field is required like id and channel: omitting it fails by name and field."""
    (tmp_path / "registry.yaml").write_text(
        "- id: auth_otp\n"
        "  channel: sms\n"
        "  priority: Q1\n"
        "  placeholders: [code]\n"
        "  variants:\n"
        '    am: "ኮድዎ {code}"\n'
        '    om: "Koodii kee {code}"\n'
        '    en: "Your code {code}"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError) as failure:
        load_templates(tmp_path)
    message = str(failure.value)
    assert "auth_otp" in message
    assert "review_status" in message
    assert "missing required field" in message


def test_a_review_status_that_is_not_a_string_is_refused_and_named(tmp_path: Path) -> None:
    """A label is a word, not a list or a number: the type error names the template too."""
    (tmp_path / "registry.yaml").write_text(
        "- id: auth_otp\n"
        "  channel: sms\n"
        "  priority: Q1\n"
        "  review_status: [approved]\n"
        "  placeholders: [code]\n"
        "  variants:\n"
        '    am: "ኮድዎ {code}"\n'
        '    om: "Koodii kee {code}"\n'
        '    en: "Your code {code}"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError) as failure:
        load_templates(tmp_path)
    message = str(failure.value)
    assert "auth_otp" in message
    assert "review_status must be a string" in message


def _write_registry(
    directory: Path,
    *,
    review_status: str = "unreviewed",
    en: str = "Your listing is live. Code {code}",
) -> None:
    """A one-template registry the round-trip test can rewrite in place."""
    (directory / "registry.yaml").write_text(
        "- id: listing_created\n"
        "  channel: sms\n"
        "  priority: Q2\n"
        f"  review_status: {review_status}\n"
        "  placeholders: [code]\n"
        "  variants:\n"
        '    am: "ማስታወቂያዎ ተለቀቀ። ኮድ {code}"\n'
        '    om: "Beeksisni kee baheera. Koodii {code}"\n'
        f'    en: "{en}"\n',
        encoding="utf-8",
    )


def _write_reviews(directory: Path, *, hash_value: str) -> None:
    """A review record for the English variant of the fixture registry."""
    (directory / "reviews.yaml").write_text(
        "reviews:\n"
        "  - template_id: listing_created\n"
        "    language: en\n"
        '    reviewer: "Ada"\n'
        "    reviewed_at: 2026-11-01\n"
        "    decision: approved\n"
        f'    hash: "{hash_value}"\n'
        '    notes: "Read against the listing confirmation."\n',
        encoding="utf-8",
    )


def test_a_matching_review_is_reviewed_and_a_changed_variant_is_stale(tmp_path: Path) -> None:
    """The hash is over the raw variant: the same words stay reviewed, new words go stale."""
    en = "Your listing is live. Code {code}"
    _write_registry(tmp_path, en=en)
    _write_reviews(tmp_path, hash_value=hash_variant(en))

    loaded = load_templates(tmp_path)["listing_created"]
    assert loaded.effective_review_status == {
        "am": "unreviewed",
        "om": "unreviewed",
        "en": "reviewed",
    }

    changed = "Your listing is now live. Code {code}"
    _write_registry(tmp_path, en=changed)
    stale = load_templates(tmp_path)["listing_created"]
    assert stale.effective_review_status["en"] == "stale"

    _write_registry(tmp_path, en=en)
    restored = load_templates(tmp_path)["listing_created"]
    assert restored.effective_review_status["en"] == "reviewed"


def test_a_variant_with_no_review_entry_or_file_is_unreviewed(tmp_path: Path) -> None:
    """A missing reviews file is not an error, and an empty record reviews nobody."""
    _write_registry(tmp_path)
    missing = load_templates(tmp_path)["listing_created"]
    assert set(missing.effective_review_status.values()) == {"unreviewed"}

    (tmp_path / "reviews.yaml").write_text("reviews: []\n", encoding="utf-8")
    empty = load_templates(tmp_path)["listing_created"]
    assert set(empty.effective_review_status.values()) == {"unreviewed"}


def test_a_spec_supplied_variant_is_trusted_even_with_a_review_entry(tmp_path: Path) -> None:
    """The specification's words are trusted regardless of a matching or mismatching hash."""
    _write_registry(tmp_path, review_status="spec_supplied")
    _write_reviews(tmp_path, hash_value="sha256:" + "0" * 64)

    loaded = load_templates(tmp_path)["listing_created"]
    assert loaded.effective_review_status == {
        "am": "spec_supplied",
        "om": "spec_supplied",
        "en": "spec_supplied",
    }


def test_a_malformed_reviews_file_is_refused_at_load_time(tmp_path: Path) -> None:
    """A review missing its required fields is a build-time failure, named by file."""
    _write_registry(tmp_path)
    (tmp_path / "reviews.yaml").write_text(
        "reviews:\n  - template_id: listing_created\n", encoding="utf-8"
    )

    with pytest.raises(ValueError) as failure:
        load_templates(tmp_path)
    assert "reviews.yaml" in str(failure.value)


def test_the_export_writes_one_row_per_template_and_language(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The worksheet covers the whole registry and pre-fills the specification's rows."""
    out = tmp_path / "translations.csv"
    assert export_translations.main(["--out", str(out)]) == 0
    summary = capsys.readouterr().out.strip()

    registry = load_templates()
    expected = sum(len(template.variants) for template in registry.values())
    assert summary == f"wrote {expected} rows to {out}"

    with out.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        assert tuple(reader.fieldnames or ()) == EXPECTED_COLUMNS
        rows = list(reader)

    assert len(rows) == expected
    assert len({(row["template_id"], row["language"]) for row in rows}) == expected
    spec_expected = sum(
        1
        for template in registry.values()
        for language in template.variants
        if template.effective_review_status[language] == "spec_supplied"
    )
    spec_rows = [row for row in rows if row["review_status_current"] == "spec_supplied"]
    assert len(spec_rows) == spec_expected
    assert all(row["decision"] == "N/A" and row["reviewer"] == "SPEC" for row in spec_rows)

    # The worksheet and the budget gate must measure the same string with the same fixtures.
    assert export_translations.PLACEHOLDER_FIXTURES == BUDGET_FIXTURES
    assert export_translations.single_segment_limit("en") == 160
    assert export_translations.single_segment_limit("om") == 160
    assert export_translations.single_segment_limit("am") == 70
    assert export_translations.single_segment_limit("ti") == 70


def test_the_hash_helper_prints_the_hash_the_loader_uses(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The printed line is the exact raw-variant hash the loader compares against."""
    template = load_templates()["listing_created"]
    assert export_translations.main(["--hash", "listing_created", "en"]) == 0
    printed = capsys.readouterr().out.strip()

    assert re.fullmatch(r"sha256:[0-9a-f]{64}", printed)
    assert printed == hash_variant(template.variants["en"])
