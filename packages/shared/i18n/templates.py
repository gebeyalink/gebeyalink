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

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

import yaml

from packages.shared.messaging.queues import SmsPriority

TEMPLATES_DIR: Final[Path] = Path(__file__).parent / "templates"

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


class TemplateReview(StrEnum):
    """Whether a template's words are the specification's or this team's, waiting on O-3."""

    SPEC_SUPPLIED = "spec_supplied"
    UNREVIEWED = "unreviewed"


@dataclass(frozen=True, slots=True)
class MessageTemplate:
    """One message, fully described: what to say, in which lane, in which languages."""

    id: str
    channel: str
    priority: SmsPriority
    placeholders: tuple[str, ...]
    variants: dict[str, str]
    review_status: TemplateReview


def load_templates(directory: Path | None = None) -> dict[str, MessageTemplate]:
    """Parse every ``templates/*.yaml`` file and key the result by template id.

    The argument exists so tests can point the loader at a throwaway directory holding a
    deliberately broken file; production always uses ``TEMPLATES_DIR``.

    Raises:
        ValueError: if a YAML file is not a list of mappings, an entry is missing a
            required field, holds a value this registry cannot use, or repeats an id.
    """
    root = directory or TEMPLATES_DIR
    registry: dict[str, MessageTemplate] = {}
    for path in sorted(root.glob("*.yaml")):
        for raw in _read_templates(path):
            template = _validate_template(path, raw)
            if template.id in registry:
                raise ValueError(f"{path}: duplicate template id {template.id!r}")
            registry[template.id] = template
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


def _validate_template(path: Path, parsed: Any) -> MessageTemplate:
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

    return MessageTemplate(
        id=template_id,
        channel=channel,
        priority=_parse_priority(template_id, path, parsed["priority"]),
        placeholders=_parse_placeholders(template_id, path, parsed["placeholders"]),
        variants=_parse_variants(template_id, path, parsed["variants"]),
        review_status=_parse_review_status(template_id, path, parsed["review_status"]),
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
    """The names a sender must supply before the message is rendered."""
    if (
        not isinstance(raw, list)
        or not raw
        or not all(isinstance(name, str) and name for name in raw)
    ):
        raise ValueError(
            f"{path}: template {template_id!r} placeholders must be a non-empty list of names"
        )
    return tuple(raw)


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
