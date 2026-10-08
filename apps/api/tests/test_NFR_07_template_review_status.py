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

from pathlib import Path

import pytest

from packages.shared.i18n.templates import TemplateReview, load_templates

pytestmark = pytest.mark.unit


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
