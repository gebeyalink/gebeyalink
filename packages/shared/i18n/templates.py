"""The registry of every text message the platform sends, loaded from YAML data.

A message is written once, in every language, and classified once, into one of three
send lanes. Both decisions live in ``templates/*.yaml`` next to this module, so a new
message class is data, not code: nobody edits a Python dict to add the next channel.

The loader is deliberately strict. A template missing a required field, a template that
names a priority the queue machine does not know, a variant in a language the SMS budget
does not measure, a review status the registry does not accept — each of those is a
build-time failure, the same kind of gate the message-parity checker runs against the UI
catalogues, so the mistake is caught by the test suite rather than by a trader reading
the cracked sentence.

Provenance is a first-class field, not a comment. The budget gate measures a string and
the lane gate classifies a string; neither can tell whether the words are the
specification's verbatim or this team's authored copy awaiting a native speaker, so every
message declares which one it is.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

import yaml

from packages.shared.i18n.languages import Language
from packages.shared.messaging.queues import SmsPriority

TEMPLATES_DIR: Final[Path] = Path(__file__).parent / "templates"
REVIEWS_FILENAME: Final[str] = "reviews.yaml"
REVIEWS_PATH: Final[Path] = Path(__file__).parent / REVIEWS_FILENAME

_REQUIRED_FIELDS: Final[tuple[str, ...]] = (
    "id",
    "channel",
    "priority",
    "placeholders",
    "variants",
    "review_status",
)
_REQUIRED_VARIANTS: Final[tuple[str, ...]] = ("am", "om", "en")
_KNOWN_VARIANTS: Final[frozenset[str]] = frozenset({"am", "om", "en", "ti"})
#: The channels this registry carries. SMS is the whole of it today; USSD is admitted because
#: the two channels share this registry and its language budget, and a USSD menu prompt is a
#: message by the same rules. A channel outside this set — push, email — is refused with its
#: name in the failure, so that "we do not carry this" is never silent.
_KNOWN_CHANNELS: Final[frozenset[str]] = frozenset({"sms", "ussd"})
_REVIEW_FIELDS: Final[tuple[str, ...]] = (
    "template_id",
    "language",
    "reviewer",
    "reviewed_at",
    "decision",
    "hash",
)
_HASH_PATTERN: Final[re.Pattern[str]] = re.compile(r"sha256:[0-9a-fA-F]{64}")


class TemplateReview(StrEnum):
    """Whether a template's words are the specification's or this team's, waiting on O-3."""

    SPEC_SUPPLIED = "spec_supplied"
    UNREVIEWED = "unreviewed"


class EffectiveReviewStatus(StrEnum):
    """What the registry knows about a variant once the reviews file is joined in.

    ``review_status`` is what the template declares; this is the answer after the review
    record is read. A declaration of ``spec_supplied`` is always trusted; otherwise the
    variant is reviewed when a matching hash exists and stale when the words have moved.
    """

    SPEC_SUPPLIED = "spec_supplied"
    UNREVIEWED = "unreviewed"
    REVIEWED = "reviewed"
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class MessageTemplate:
    """One message, fully described: what to say, in which lane, in which languages.

    ``review_status`` labels the template as a whole; ``variant_review_status`` is the
    per-language provenance and is always complete. When a template's languages do not all
    share one provenance — the specification supplies the English and the Amharic of a menu
    while the Oromo is this team's draft — the map says which variant is which, and a lone
    label would have had to lie about two of the three.

    ``effective_review_status`` is the same question answered against the review record: a
    variant is ``spec_supplied`` when the template says so, otherwise it is ``reviewed`` if a
    matching hash exists, ``stale`` if the words changed after review, and ``unreviewed`` if
    nobody has looked at it yet.
    """

    id: str
    channel: str
    priority: SmsPriority
    placeholders: tuple[str, ...]
    variants: dict[str, str]
    review_status: TemplateReview
    variant_review_status: dict[str, TemplateReview]
    effective_review_status: dict[str, str]


@dataclass(frozen=True, slots=True)
class ReviewEntry:
    """One reviewer's record of one variant, as stored in ``reviews.yaml``."""

    template_id: str
    language: str
    reviewer: str
    reviewed_at: str
    decision: str
    content_hash: str
    notes: str


def hash_variant(text: str) -> str:
    """The hash ``reviews.yaml`` records for a raw variant, in ``sha256:...`` form.

    The hash is over the words the reviewer read — the placeholder-bearing template
    string, not the rendered copy. A fixture change therefore leaves the review standing;
    an edit to the template text makes it stale.
    """
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_templates(directory: Path | None = None) -> dict[str, MessageTemplate]:
    """Parse every ``templates/*.yaml`` file and key the result by template id.

    The argument exists so tests can point the loader at a throwaway directory holding a
    deliberately broken file; production always uses ``TEMPLATES_DIR``. A custom directory
    keeps its ``reviews.yaml`` beside its template files, which is what the round-trip test
    needs; production reads the package's fixed ``REVIEWS_PATH``.

    Raises:
        ValueError: if a YAML file is not a list of mappings, an entry is missing a
            required field, holds a value this registry cannot use, repeats an id, or the
            reviews file is malformed or names a template or language this registry does not
            carry.
    """
    root = directory or TEMPLATES_DIR
    reviews_path = REVIEWS_PATH if directory is None else directory / REVIEWS_FILENAME
    reviews = _load_reviews(reviews_path)
    registry: dict[str, MessageTemplate] = {}
    for path in sorted(root.glob("*.yaml")):
        for raw in _read_templates(path):
            template = _validate_template(path, raw, reviews)
            if template.id in registry:
                raise ValueError(f"{path}: duplicate template id {template.id!r}")
            registry[template.id] = template
    _validate_review_entries(registry, reviews, reviews_path)
    return registry


def _read_templates(path: Path) -> list[Any]:
    """Read one YAML file, insisting it holds a list of template entries."""
    try:
        with path.open(encoding="utf-8") as handle:
            parsed: Any = yaml.safe_load(handle)
    except yaml.YAMLError as error:
        raise ValueError(f"{path}: could not be read as YAML ({error})") from error
    if not isinstance(parsed, list):
        raise ValueError(f"{path}: expected a list of templates, got {type(parsed).__name__}")
    return parsed


def _load_reviews(path: Path) -> dict[tuple[str, str], ReviewEntry]:
    """Read the review record, or answer with none when the file is absent.

    The record is deliberately strict: a review that lost its hash, named a decision this
    workflow does not use, or repeated a variant is a build-time mistake, not something to
    paper over at read time. A missing file is not a mistake — it means nobody has reviewed
    anything yet.
    """
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as handle:
            parsed: Any = yaml.safe_load(handle)
    except yaml.YAMLError as error:
        raise ValueError(f"{path}: could not be read as YAML ({error})") from error
    if not isinstance(parsed, dict):
        raise ValueError(f"{path}: expected a mapping with a 'reviews' list")
    raw_reviews = parsed.get("reviews")
    if not isinstance(raw_reviews, list):
        raise ValueError(f"{path}: 'reviews' must be a list")

    reviews: dict[tuple[str, str], ReviewEntry] = {}
    for index, raw in enumerate(raw_reviews, start=1):
        entry = _parse_review_entry(path, index, raw)
        key = (entry.template_id, entry.language)
        if key in reviews:
            raise ValueError(
                f"{path}: review {index} repeats {entry.template_id!r}/{entry.language!r}"
            )
        reviews[key] = entry
    return reviews


def _parse_review_entry(path: Path, index: int, raw: Any) -> ReviewEntry:
    """One review entry, with every field this workflow needs and no guessed defaults."""
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: review {index} must be a mapping")
    missing = [name for name in _REVIEW_FIELDS if name not in raw]
    if missing:
        raise ValueError(
            f"{path}: review {index} is missing required field(s): {', '.join(missing)}"
        )

    template_id = raw["template_id"]
    if not isinstance(template_id, str) or not template_id:
        raise ValueError(f"{path}: review {index} template_id must be a non-empty string")
    language = raw["language"]
    if not isinstance(language, str) or not language:
        raise ValueError(f"{path}: review {index} language must be a non-empty string")
    reviewer = raw["reviewer"]
    if not isinstance(reviewer, str) or not reviewer:
        raise ValueError(f"{path}: review {index} reviewer must be a non-empty string")
    decision = raw["decision"]
    if decision not in {"approved", "revise"}:
        raise ValueError(
            f"{path}: review {index} decision must be 'approved' or 'revise', got {decision!r}"
        )
    content_hash = raw["hash"]
    if not isinstance(content_hash, str) or not _HASH_PATTERN.fullmatch(content_hash):
        raise ValueError(
            f"{path}: review {index} hash must be 'sha256:' followed by 64 hex characters"
        )
    notes = raw.get("notes", "")
    if not isinstance(notes, str):
        raise ValueError(f"{path}: review {index} notes must be a string")
    return ReviewEntry(
        template_id=template_id,
        language=language,
        reviewer=reviewer,
        reviewed_at=_parse_reviewed_at(path, index, raw["reviewed_at"]),
        decision=decision,
        content_hash=content_hash,
        notes=notes,
    )


def _parse_reviewed_at(path: Path, index: int, raw: Any) -> str:
    """A review date, accepted as YAML's date or as its ISO-8601 string form."""
    if isinstance(raw, datetime):
        return raw.date().isoformat()
    if isinstance(raw, date):
        return raw.isoformat()
    if isinstance(raw, str):
        try:
            return date.fromisoformat(raw).isoformat()
        except ValueError as error:
            raise ValueError(f"{path}: review {index} reviewed_at is not an ISO date") from error
    raise ValueError(f"{path}: review {index} reviewed_at must be a date")


def _validate_review_entries(
    registry: dict[str, MessageTemplate],
    reviews: dict[tuple[str, str], ReviewEntry],
    path: Path,
) -> None:
    """Every review must name a template and language the registry actually carries."""
    for template_id, language in reviews:
        template = registry.get(template_id)
        if template is None:
            raise ValueError(f"{path}: review names unknown template {template_id!r}")
        if language not in template.variants:
            raise ValueError(
                f"{path}: review names unknown language {language!r} for template {template_id!r}"
            )


def _effective_review_status(
    template_id: str,
    variants: dict[str, str],
    variant_review_status: dict[str, TemplateReview],
    reviews: dict[tuple[str, str], ReviewEntry],
) -> dict[str, str]:
    """Join the template's declared provenance with the review record, per language."""
    effective: dict[str, str] = {}
    for language, text in variants.items():
        if variant_review_status[language] is TemplateReview.SPEC_SUPPLIED:
            effective[language] = EffectiveReviewStatus.SPEC_SUPPLIED.value
            continue
        entry = reviews.get((template_id, language))
        if entry is None:
            effective[language] = EffectiveReviewStatus.UNREVIEWED.value
        elif entry.content_hash.lower() == hash_variant(text):
            effective[language] = EffectiveReviewStatus.REVIEWED.value
        else:
            effective[language] = EffectiveReviewStatus.STALE.value
    return effective


def _validate_template(
    path: Path,
    parsed: Any,
    reviews: dict[tuple[str, str], ReviewEntry],
) -> MessageTemplate:
    """Turn one parsed YAML entry into a template, or refuse it with the reasons why."""
    if not isinstance(parsed, dict):
        raise ValueError(f"{path}: every template must be a mapping, got {type(parsed).__name__}")
    missing = [name for name in _REQUIRED_FIELDS if name not in parsed]
    if missing:
        entry_id = parsed.get("id")
        label = f"template {entry_id!r}" if isinstance(entry_id, str) and entry_id else "an entry"
        raise ValueError(f"{path}: {label} is missing required field(s): {', '.join(missing)}")

    template_id = parsed["id"]
    if not isinstance(template_id, str) or not template_id:
        raise ValueError(f"{path}: template id must be a non-empty string, got {template_id!r}")

    channel = parsed["channel"]
    if channel not in _KNOWN_CHANNELS:
        raise ValueError(
            f"{path}: template {template_id!r} declares channel {channel!r}; "
            f"this registry knows only {', '.join(sorted(_KNOWN_CHANNELS))}"
        )

    variants = _parse_variants(template_id, path, parsed["variants"])
    review_status = _parse_review_status(template_id, path, parsed["review_status"])
    variant_review_status = _parse_variant_review_status(
        template_id, path, variants, review_status, parsed.get("variant_review_status")
    )
    return MessageTemplate(
        id=template_id,
        channel=channel,
        priority=_parse_priority(template_id, path, parsed["priority"]),
        placeholders=_parse_placeholders(template_id, path, parsed["placeholders"]),
        variants=variants,
        review_status=review_status,
        variant_review_status=variant_review_status,
        effective_review_status=_effective_review_status(
            template_id, variants, variant_review_status, reviews
        ),
    )


def _parse_priority(template_id: str, path: Path, raw: Any) -> SmsPriority:
    """A declared send lane, or the set of lanes this registry knows."""
    if not isinstance(raw, str):
        raise ValueError(f"{path}: template {template_id!r} priority must be a string")
    for priority in SmsPriority:
        if priority.value == raw:
            return priority
    raise ValueError(
        f"{path}: template {template_id!r} declares unknown priority {raw!r} "
        f"(expected one of {', '.join(priority.value for priority in SmsPriority)})"
    )


def _parse_review_status(template_id: str, path: Path, raw: Any) -> TemplateReview:
    """A declared copy provenance, or a refusal that names the template and the value.

    The registry admits exactly two labels: words the specification supplies verbatim, and
    words that have not yet been read by a native speaker. Anything else — ``approved``,
    ``translated``, a review workflow's vocabulary — is not provenance, and is refused
    here rather than silently treated as one of the two real labels.
    """
    if not isinstance(raw, str):
        raise ValueError(
            f"{path}: template {template_id!r} review_status must be a string, got {raw!r}"
        )
    for status in TemplateReview:
        if status.value == raw:
            return status
    raise ValueError(
        f"{path}: template {template_id!r} declares unknown review_status {raw!r} "
        f"(expected one of {', '.join(status.value for status in TemplateReview)})"
    )


def _parse_placeholders(template_id: str, path: Path, raw: Any) -> tuple[str, ...]:
    """The names a sender must supply before the message is rendered.

    A static prompt names none and says so with an empty list. The message below is kept
    exactly as Task 15a pinned it, even though the check now admits ``[]``: the contract the
    earlier test asserts is unchanged, and a string or a missing field is still refused.
    """
    if not isinstance(raw, list) or not all(isinstance(name, str) and name for name in raw):
        raise ValueError(
            f"{path}: template {template_id!r} placeholders must be a non-empty list of names"
        )
    return tuple(raw)


def _parse_variant_review_status(
    template_id: str,
    path: Path,
    variants: dict[str, str],
    review_status: TemplateReview,
    raw: Any,
) -> dict[str, TemplateReview]:
    """The provenance of each language variant, complete or not written at all.

    When a template's languages do not all share one provenance, this map says which variant
    is which. It must be complete — every variant language a key — because a language left
    out would be a language whose words carry no honest label, which is the very thing this
    field exists to prevent. A map naming a language the template does not carry is a typo
    and is refused by name. When the field is absent, the template's single ``review_status``
    labels every variant.
    """
    if raw is None:
        return dict.fromkeys(variants, review_status)
    if not isinstance(raw, dict):
        raise ValueError(
            f"{path}: template {template_id!r} variant_review_status must map a language to "
            f"its provenance, got {type(raw).__name__}"
        )
    provided = set(raw)
    expected = set(variants)
    missing = sorted(expected - provided)
    if missing:
        raise ValueError(
            f"{path}: template {template_id!r} variant_review_status is missing "
            f"variant(s): {', '.join(missing)}"
        )
    unknown = sorted(provided - expected)
    if unknown:
        raise ValueError(
            f"{path}: template {template_id!r} variant_review_status names unknown "
            f"variant(s): {', '.join(unknown)}"
        )
    return {
        language: _parse_review_status(template_id, path, value) for language, value in raw.items()
    }


def _parse_variants(template_id: str, path: Path, raw: Any) -> dict[str, str]:
    """The per-language text, insisting am, om and en are all present."""
    if not isinstance(raw, dict):
        raise ValueError(
            f"{path}: template {template_id!r} variants must map a language to its text"
        )
    missing = [language for language in _REQUIRED_VARIANTS if language not in raw]
    if missing:
        raise ValueError(
            f"{path}: template {template_id!r} is missing required variant(s): "
            f"{', '.join(missing)}"
        )
    unknown = sorted(set(raw) - _KNOWN_VARIANTS)
    if unknown:
        raise ValueError(
            f"{path}: template {template_id!r} declares unknown variant language(s): "
            f"{', '.join(unknown)}"
        )
    if not all(isinstance(text, str) and text for text in raw.values()):
        raise ValueError(f"{path}: template {template_id!r} variants must all be non-empty strings")
    return dict(raw)


def render_template(template_id: str, language: Language, **fields: str) -> tuple[str, str]:
    """Fill one template's variant and answer with the language its words are actually in.

    The registry is the one place a message's words live, so every producer renders through
    here rather than formatting a sentence of its own: a message and its template cannot drift
    if there is only one of them. The second value is the language of the words returned, not
    the one asked for — when a template carries no variant for the requested language the
    English fallback is returned and named, because a row stores which language it holds and
    those are two different questions the moment the fallback fires.

    Raises:
        KeyError: if the registry carries no template by that id.
    """
    template = load_templates()[template_id]
    rendered_language = language.value if language.value in template.variants else Language.EN.value
    return template.variants[rendered_language].format(**fields), rendered_language
