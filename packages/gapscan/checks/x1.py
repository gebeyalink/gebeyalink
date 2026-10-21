"""X1 — a requirement ID or a spec citation in production code.

The naming checklist's rule is that a stranger must be able to read a service and understand
it with no knowledge of requirement numbering. A comment that says ``# FR-1903: check
availability`` is unreadable to that stranger, and it couples the code to a document that is
not in the repository. The same goes for a section citation (``§12.3``): it points at a
document the reader cannot open. The rule is that the comment states the rule in domain
language instead.

Tests are the allowed home of a requirement ID — that is where the ID is the point — and the
ledger is its home by definition. Migrations are allowed: a migration's comments reach for the
requirement that motivated a column, and that is a pointer, not a dependency. ``packages/`` is
production code and is checked like ``apps/api``.
"""

from __future__ import annotations

import re

from packages.gapscan.config import Context, Finding, Severity

CHECK = "X1"

#: A requirement ID, in either the canonical hyphenated form or the underscore form a test
#: would use.
REQUIREMENT_ID = re.compile(r"\b(?:FR|SEC|NFR|PRV)[-_]\d+[A-Z]?\b")

#: A spec section citation: ``§12.3``, ``§5.2`` — even the bare symbol points outside the repo.
SECTION_CITATION = re.compile(r"§\s*\d+(?:\.\d+)*")

#: A path segment that exempts a file: tests carry IDs by design, migrations are pointers, and
#: the ledger is the IDs' home.
_EXEMPT_SEGMENTS = frozenset({"tests", "traceability", "alembic"})

#: The one production file that may carry IDs, named rather than pattern-matched so the
#: exemption is visible: the scan's own milestone scope. It is a table of the requirements the
#: gate is responsible for — the same kind of object as the ledger, which is the IDs' home — and
#: an ID there is data the tool reads, not a citation a stranger is expected to look up. Every
#: other file, this package's included, is checked like ``apps/api``.
_EXEMPT_FILES = frozenset({"packages/gapscan/config.py"})


def check(context: Context) -> list[Finding]:
    """Every requirement ID and section citation in production source, with file and line."""
    findings: list[Finding] = []
    for path in context.production_files:
        if any(segment in _EXEMPT_SEGMENTS for segment in path.parts):
            continue
        if path.as_posix().endswith(tuple(_EXEMPT_FILES)):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for line_number, line in enumerate(text.splitlines(), 1):
            for pattern, label in (
                (REQUIREMENT_ID, "forbidden requirement ID in production code"),
                (SECTION_CITATION, "spec section citation in production code"),
            ):
                match = pattern.search(line)
                if match is None:
                    continue
                findings.append(
                    Finding(
                        check=CHECK,
                        severity=Severity.BLOCKING,
                        requirement=match.group(0),
                        message=label,
                        evidence=f"{path.as_posix()}:{line_number}",
                    )
                )
    return findings
