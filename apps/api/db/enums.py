"""Shared helpers for enum columns.

An enum column can store either the member names or the member values. Where those differ,
picking one deliberately keeps the column contents identical to what the API sends and what
the database checks, instead of leaving a mismatch for someone to find later.
"""

from __future__ import annotations

import enum


def stored_values(enum_class: type[enum.Enum]) -> list[str]:
    """The values as they are written to the database."""
    return [str(member.value) for member in enum_class]
