"""G1 — a requirement in scope with nothing built and no recorded reason.

The check is about silence. A row with any implemented layer is being worked on; a row with a
``blocking_issue`` is being worked on and says why not yet. A row in scope with neither is a
requirement nobody is holding, which is exactly the "forgotten requirement" this tool exists
to catch.
"""

from __future__ import annotations

from packages.gapscan.config import Context, Finding, Severity

#: The layers whose presence means "something exists". Frontend, integration and the rest are
#: downstream of one of these, so a DONE there without one of these is drift, not progress.
IMPLEMENTATION_LAYERS = ("db_status", "backend_status", "api_status", "frontend_status")

CHECK = "G1"


def check(context: Context) -> list[Finding]:
    """One finding per in-scope requirement with no DONE layer and no blocking issue."""
    findings: list[Finding] = []
    for row in context.matrix_rows:
        requirement = row["requirement_id"]
        if requirement not in context.milestone.in_scope:
            continue
        implemented = any(row.get(layer) == "DONE" for layer in IMPLEMENTATION_LAYERS)
        blocking_issue = (row.get("blocking_issue") or "").strip()
        if implemented or blocking_issue:
            continue
        findings.append(
            Finding(
                check=CHECK,
                severity=Severity.BLOCKING,
                requirement=requirement,
                message="in scope for the milestone but no layer DONE and no blocking issue",
                evidence=f"traceability/MATRIX.csv:{requirement}",
            )
        )
    return findings
