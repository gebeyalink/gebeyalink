"""G2 — layer drift: a row claiming COMPLETE while a layer it lists is not done.

This is the check the whole tool exists for. "Complete" is a claim about every layer the
requirement touches, not about the fact that some code exists. For a row whose
``overall_status`` is ``COMPLETE``, every layer named in ``applicable_layers`` must be ``DONE``
or an explicit ``N/A``; a layer that is neither is a drift finding naming that layer and its
status, and the row's own overall status is flagged as unsupported by its layers.

The asymmetry is deliberate and is the spec's: the check fails a claim of ``COMPLETE`` that the
layers do not support, and does not fail a row that is more conservative than its layers — a
row marked ``IN_PROGRESS`` while every layer reads ``DONE`` is a builder holding a slice open,
not a claim outrunning its evidence, and the tool has no business overriding that judgement. It
mechanically enforces AGENT-BRIEF's "not complete because code exists" and nothing more.
"""

from __future__ import annotations

from packages.gapscan.config import Context, Finding, Severity

CHECK = "G2"

#: The layer the ledger names -> the status column that carries it. The ledger has ten layers
#: (L1 database … L10 end-to-end) and eight status columns; L3 (the service code path) and L8
#: (validation and error handling) are both statements about the backend column, so they fold
#: into it rather than inventing a column the ledger does not have.
LAYER_COLUMNS: dict[str, str] = {
    "L1": "db_status",
    "L2": "backend_status",
    "L3": "backend_status",
    "L4": "api_status",
    "L5": "frontend_status",
    "L6": "integration_status",
    "L7": "authorization_status",
    "L8": "backend_status",
    "L9": "test_status",
    "L10": "e2e_status",
}

#: A layer is satisfied when its status is DONE or an explicit N/A. Anything else — including
#: the empty string a half-filled row carries — is not satisfied.
SATISFYING = frozenset({"DONE", "N/A"})


def applicable_layers(raw: str) -> list[str]:
    """The ledger's layer list, split and stripped, ignoring anything that is not a layer.

    ``applicable_layers`` holds ``L1;L2;L9`` and, on the goal rows, a trailing
    ``phase-exit-verification`` note. Only L1 to L10 are layers; the note is not one and has no
    status column to check.
    """
    return [part.strip() for part in raw.split(";") if part.strip() in LAYER_COLUMNS]


def check(context: Context) -> list[Finding]:
    """Drift on COMPLETE rows, and a COMPLETE that the layers do not support."""
    findings: list[Finding] = []
    for row in context.matrix_rows:
        requirement = row["requirement_id"]
        layers = applicable_layers(row.get("applicable_layers") or "")
        unsatisfied = [
            (layer, (row.get(LAYER_COLUMNS[layer]) or "").strip())
            for layer in layers
            if (row.get(LAYER_COLUMNS[layer]) or "").strip() not in SATISFYING
        ]
        recorded = (row.get("overall_status") or "").strip()

        if recorded != "COMPLETE":
            continue

        for layer, status in unsatisfied:
            column = LAYER_COLUMNS[layer]
            findings.append(
                Finding(
                    check=CHECK,
                    severity=Severity.BLOCKING,
                    requirement=requirement,
                    message=f"overall=COMPLETE but {layer} ({column})={status or 'blank'}",
                    evidence=f"traceability/MATRIX.csv:{requirement}",
                )
            )
        if unsatisfied:
            findings.append(
                Finding(
                    check=CHECK,
                    severity=Severity.BLOCKING,
                    requirement=requirement,
                    message="overall_status=COMPLETE but its layers do not recompute to COMPLETE "
                    f"({len(unsatisfied)} layer(s) unsatisfied)",
                    evidence=f"traceability/MATRIX.csv:{requirement}",
                )
            )
    return findings
