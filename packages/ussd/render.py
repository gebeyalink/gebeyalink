"""Turning a menu node into the words a feature phone shows.

A node names the registry entries it renders and the context that fills their placeholders;
this module joins those entries into one screen. Nothing here decides *which* menu is owed —
that is the engine — and nothing here knows what a listing is — that is the gateway. It only
looks a template up, picks the trader's language, substitutes, and joins the parts.

Two conventions are pinned here rather than repeated in the trees. The first is the error
marker: §14.1 shows a refused choice as a line beginning ``❌``, and that marker is a constant
of this renderer so no template has to carry a symbol that is not part of its sentence. The
second is the fallback: a variant a language does not carry falls back to English, because an
untranslated menu must still render rather than crash a call mid-session.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from packages.shared.i18n.languages import DEFAULT_LANGUAGE, Language
from packages.shared.i18n.templates import MessageTemplate, load_templates

#: The marker every refused choice begins with (SPEC §14.1). Held here, not in the copy, so
#: the reason a template holds is a sentence and the convention is written down once.
ERROR_MARKER = "❌ "


class UnknownTemplateError(LookupError):
    """A node named a registry entry that does not exist.

    Raised while the trees are built, not while a trader is waiting: an unknown id is a bug in
    this repository and must fail the build rather than render an empty line on a handset.
    """


def _variant(template: MessageTemplate, language: Language | None) -> str:
    chosen = language or DEFAULT_LANGUAGE
    return template.variants.get(chosen.value) or template.variants[Language.EN.value]


def render(template_id: str, language: Language | None, context: dict[str, Any]) -> str:
    """Render one registry entry in the trader's language with the given values."""
    registry = load_templates()
    try:
        template = registry[template_id]
    except KeyError as error:
        raise UnknownTemplateError(template_id) from error
    return _variant(template, language).format(**context)


def render_screen(
    template_ids: Sequence[str], language: Language | None, context: dict[str, Any]
) -> str:
    """Render a node's entries as the one screen the trader reads, one below the other."""
    return "\n".join(render(template_id, language, context) for template_id in template_ids)
