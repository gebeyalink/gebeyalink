"""Language preferences and message lookup.

Every user picks one of three languages and every message the platform sends them —
error sentences and text messages alike — is written in that language. Amharic is the
default because it is what most traders read first; Oromo ships alongside it from
launch, English as the fallback for wider reach.

Fallback order is deliberate: a missing Amharic string falls back to English rather than
raising, because an untranslated error must never turn into a crashed request.
"""

from __future__ import annotations

from enum import StrEnum


class Language(StrEnum):
    """Languages a user may choose, stored per account."""

    AM = "am"
    OM = "om"
    EN = "en"


#: Locales the platform can speak, in the order a new deployment enables them.
SUPPORTED_LANGUAGES: tuple[Language, ...] = (Language.AM, Language.OM, Language.EN)

#: What a user gets when they have not chosen. Amharic is read by most traders, so it is
#: the right guess for somebody we have never spoken to.
DEFAULT_LANGUAGE = Language.AM


def parse_language(raw: str | None) -> Language | None:
    """Read a language tag, tolerating region suffixes and mixed case.

    ``en-ET`` and ``en`` mean the same thing here; the region carries no signal we act on.
    Returns ``None`` for anything unrecognised so the caller can decide whether the input
    was a preference worth storing or a header worth ignoring.
    """
    if not raw:
        return None
    tag = raw.strip().lower().replace("_", "-").split("-", maxsplit=1)[0]
    try:
        return Language(tag)
    except ValueError:
        return None


def resolve_language(raw: str | None) -> Language:
    """Read a language tag and fall back to the platform default."""
    return parse_language(raw) or DEFAULT_LANGUAGE
