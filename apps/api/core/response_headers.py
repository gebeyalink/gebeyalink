"""Headers that every response this API produces carries.

They live here rather than inline in the request middleware because there are two places a
response can be born, and only one of them is inside that middleware.

A response that comes back through the stack - anything a route returned, and every failure
handled by `ExceptionMiddleware`, which sits below the middleware - can be stamped on the way
out. A response born from an *unhandled* exception cannot: Starlette routes a handler
registered for `Exception` to `ServerErrorMiddleware`, which wraps the middleware rather than
sitting inside it, so a 500 never passes through it. Registering the header in both places is
not belt and braces, it is the only arrangement that covers both.

The header is `X-Content-Type-Options: nosniff`, which stops a browser second-guessing a
declared `Content-Type`. Nothing in this API serves user-supplied content, so it costs
nothing, and it removes the question of what a browser would do with a response that claims to
be JSON and is not.
"""

from __future__ import annotations

from typing import Final

#: Applied to every response, by the middleware and by the error layer.
SECURITY_HEADERS: Final[dict[str, str]] = {"X-Content-Type-Options": "nosniff"}


def apply_security_headers(headers: dict[str, str]) -> None:
    """Merge the policy into a header mapping, without overwriting a deliberate choice."""
    for name, value in SECURITY_HEADERS.items():
        headers.setdefault(name, value)
