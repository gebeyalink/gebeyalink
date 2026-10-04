"""The page shape every cursor-paginated endpoint answers with.

A client that has to guess whether a list came back bare, or wrapped, or with a separate
total count, will guess wrong on one of them. So the wrapper is declared once here and every
paginated endpoint answers in it, whatever it is paginating.

There is no total count. Counting the rows behind a filtered marketplace query costs more
than the page does, and the number goes stale the moment it is computed; a cursor and a
boolean saying whether more exists are both cheap and both true.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class CursorPage[T](BaseModel):
    """One page of results and the cursor that asks for the next one."""

    model_config = ConfigDict(extra="forbid")

    data: list[T]
    #: Opaque to the caller, which must echo it back rather than read it. Null on the last
    #: page, which is also what makes it the signal to stop.
    next_cursor: str | None = None
    #: True when there is at least one more row beyond this page. Distinct from
    #: ``next_cursor`` being present so a client can tell "no more" from "could not say".
    has_more: bool = Field(default=False)
