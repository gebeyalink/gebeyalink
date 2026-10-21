"""X2 — a planning-style name anywhere in the tree.

``module4/``, ``phase2_entities.py``, ``task7.md`` — names that describe a conversation rather
than a responsibility. A stranger browsing the repository sees the plan and not the product.
The rule is the naming checklist's: a name states what a thing is, not when it was built or
which numbered package produced it.

Test files are exempt: ``test_FR_601_...py`` carries the requirement ID on purpose, and the ID
pattern here would otherwise match it. The walk covers the whole repository, so a directory is
caught as a directory and a file as a file.
"""

from __future__ import annotations

import re

from packages.gapscan.config import Context, Finding, Severity

CHECK = "X2"

#: A planning-style name segment. ``fr-\d`` matches case-insensitively; the rest are the
#: numbered-module/phase/task/stage vocabulary the naming checklist forbids.
FORBIDDEN_NAME = re.compile(r"(module\d+|phase\d+|task\d+|stage\d+|fr-\d+|agentstep\d+)", re.I)


def check(context: Context) -> list[Finding]:
    """Every file or directory whose name carries a planning-style segment."""
    findings: list[Finding] = []
    for path in sorted(context.tree_names):
        name = path.rsplit("/", 1)[-1]
        if name.startswith("test_"):
            continue
        match = FORBIDDEN_NAME.search(name)
        if match is None:
            continue
        findings.append(
            Finding(
                check=CHECK,
                severity=Severity.BLOCKING,
                requirement="—",
                message=f"planning-style name '{match.group(0)}'",
                evidence=path,
            )
        )
    return findings
