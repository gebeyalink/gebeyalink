"""Export the O-3 translation review worksheet, and hash one variant for the record.

    python -m apps.api.management.export_translations --out exports/translations.csv

The worksheet is one CSV row per (template, language). It carries the rendered copy and the
length the budget gate measures, so a reviewer reads the same string the platform will send
rather than the placeholder source. The review columns are blank for somebody to fill in;
``spec_supplied`` rows are pre-filled because the specification already supplies those words.

The ``--hash`` mode prints the one line a reviewer's response needs to become a record:

    python -m apps.api.management.export_translations --hash listing_created en

There is deliberately no ``--apply``: the owner merges the first batch into
``packages/shared/i18n/reviews.yaml`` by hand. A later slice can add the merge once the shape
of the responses is known.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from packages.shared.i18n.templates import (
    EffectiveReviewStatus,
    MessageTemplate,
    hash_variant,
    load_templates,
)

CSV_COLUMNS: Final[tuple[str, ...]] = (
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

DEFAULT_OUT: Final[Path] = Path("exports") / "translations.csv"

#: Kept identical to the budget gate's fixtures in
#: ``apps/api/tests/test_NFR_07_sms_budget.py`` and asserted equal by the review-status test,
#: so ``rendered_length`` and ``within_budget`` measure the same string CI measures.
PLACEHOLDER_FIXTURES: Final[dict[str, str]] = {
    "code": "123456",
    "order_code": "GL-123456",
    "quantity_kg": "1000",
    "crop": "Teff",
    "price": "2450",
    "ussd": "*851#",
    "price_per_quintal": "4850",
    "field": "quantity_kg",
    "status": "READY_FOR_PICKUP",
    "count": "0",
    "token": "A1B2C3D4",
    "missing": "address",
    "active_count": "12",
    "paused_count": "3",
    "rows": "1. Teff 1000kg @2450 ETB — Adaa (12km)",
    "next": "2. Next page",
    "last_used": "Oromia",
    "region": "Oromia",
    "prices": "Teff 4,850/q",
    "place": "Adaa",
    "distance": " (12km)",
    "pickup_code": "493821",
}

_LATIN_LANGUAGES: Final[frozenset[str]] = frozenset({"en", "om"})
_GEEZ_LANGUAGES: Final[frozenset[str]] = frozenset({"am", "ti"})


def single_segment_limit(language: str) -> int:
    """The longest rendered message, in characters, that fits one SMS segment."""
    if language in _LATIN_LANGUAGES:
        return 160
    if language in _GEEZ_LANGUAGES:
        return 70
    raise ValueError(f"no SMS budget is declared for language {language!r}")


def within_single_segment(rendered: str, language: str) -> bool:
    """Whether a rendered message fits its language's single-segment budget."""
    return len(rendered) <= single_segment_limit(language)


def _render(template: MessageTemplate, language: str) -> str:
    """Render one variant with the budget gate's fixture values."""
    unknown = sorted(set(template.placeholders) - set(PLACEHOLDER_FIXTURES))
    if unknown:
        raise ValueError(f"no placeholder fixture declared for {template.id}: {unknown}")
    fixtures = {name: PLACEHOLDER_FIXTURES[name] for name in template.placeholders}
    return template.variants[language].format(**fixtures)


def _rows(registry: Mapping[str, MessageTemplate]) -> list[dict[str, str]]:
    """One worksheet row per template and language, in stable id/language order."""
    rows: list[dict[str, str]] = []
    for template_id in sorted(registry):
        template = registry[template_id]
        for language in sorted(template.variants):
            rendered = _render(template, language)
            budget = single_segment_limit(language)
            status = template.effective_review_status[language]
            spec_supplied = status == EffectiveReviewStatus.SPEC_SUPPLIED.value
            rows.append(
                {
                    "template_id": template_id,
                    "channel": template.channel,
                    "priority": template.priority.value,
                    "language": language,
                    "rendered_copy": rendered,
                    "placeholders": ", ".join(template.placeholders),
                    "rendered_length": str(len(rendered)),
                    "budget": str(budget),
                    "within_budget": (
                        "true" if within_single_segment(rendered, language) else "false"
                    ),
                    "review_status_current": status,
                    "reviewer": "SPEC" if spec_supplied else "",
                    "reviewed_at": "",
                    "decision": "N/A" if spec_supplied else "",
                    "revised_text": "",
                    "notes": "",
                }
            )
    return rows


def export(path: Path) -> int:
    """Write the worksheet and print a one-line summary of what was written."""
    rows = _rows(load_templates())
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {len(rows)} rows to {path}")
    return 0


def _print_hash(template_id: str, language: str) -> int:
    """Print one variant's ``sha256:...`` review hash, or explain why it cannot."""
    registry = load_templates()
    template = registry.get(template_id)
    if template is None:
        print(f"unknown template id: {template_id}", file=sys.stderr)
        return 1
    if language not in template.variants:
        print(f"template {template_id!r} has no variant {language!r}", file=sys.stderr)
        return 1
    print(hash_variant(template.variants[language]))
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export the translation review worksheet.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help=f"Where to write the CSV (default: {DEFAULT_OUT}).",
    )
    mode.add_argument(
        "--hash",
        nargs=2,
        metavar=("TEMPLATE_ID", "LANGUAGE"),
        help="Print one variant's review hash instead of exporting.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    if args.hash is not None:
        return _print_hash(args.hash[0], args.hash[1])
    return export(args.out)


if __name__ == "__main__":
    raise SystemExit(main())
