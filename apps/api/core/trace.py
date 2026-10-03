"""Per-request context that survives down the call stack.

Two things need to travel with a request without being threaded through every function
signature: an identifier to quote in a support ticket, and the language the caller reads.
Both are held in context variables, which are scoped to the request being handled, so a
worker thread serving someone else's request never sees them.
"""

from __future__ import annotations

from contextvars import ContextVar
from uuid import uuid4

from packages.shared.i18n.languages import DEFAULT_LANGUAGE, Language, parse_language

_trace_id: ContextVar[str | None] = ContextVar("trace_id", default=None)
_language: ContextVar[Language] = ContextVar("language", default=DEFAULT_LANGUAGE)


def new_trace_id() -> str:
    """Mint the identifier that ties a user's report to this exact attempt."""
    return str(uuid4())


def set_trace_id(trace_id: str) -> None:
    _trace_id.set(trace_id)


def get_trace_id() -> str:
    """The current request's identifier, or a fresh one outside a request."""
    return _trace_id.get() or new_trace_id()


def set_language(language: Language) -> None:
    """Record the language subsequent messages in this request should use.

    Called twice on some paths: once from the caller's stated preference, then again from
    the stored preference once we know who they are. The stored one wins, because it is
    what they chose when they signed up.
    """
    _language.set(language)


def set_language_from_header(header: str | None) -> None:
    language = parse_language(header)
    if language is not None:
        _language.set(language)


def get_language() -> Language:
    """The language to answer in. Amharic unless somebody said otherwise."""
    return _language.get()
