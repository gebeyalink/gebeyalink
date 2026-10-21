"""G3 — a user-facing requirement whose frontend layer is claimed but unbuilt.

Phase 1 has no mobile app; its user-facing surface is USSD and SMS. So the artifact this check
looks for is a USSD tree or an SMS/USSD journey test, not a screen — and the artifact has to
be a real one. The claim being tested is narrow: if the ledger says the frontend layer is DONE,
something in the tree is that frontend and it reaches the contract.

A requirement whose frontend layer is ``N/A`` or ``NOT_STARTED`` is not making that claim and is
skipped; the check is about a claim outrunning its evidence, not about work not yet begun.

The artifact is matched two ways because the naming rule forces it. A USSD tree module is
production code and may not carry a requirement ID, so a tree is matched by the requirement
having a test that drives the USSD/SMS surface — the test carries the ID by design. An SMS
command test carries the ID in its own name and drives the surface through the inbound-SMS
fixture. Either counts; a requirement with neither, while claiming a DONE frontend, is drift.
"""

from __future__ import annotations

from packages.gapscan.config import Context, Finding, Severity

CHECK = "G3"

#: Markers that a test file drives the Phase 1 user-facing surface. The fixture names are the
#: repository's own — ``sms_inbound`` sends an SMS command, ``RecordingSmsProvider`` captures
#: outbound SMS, and a USSD journey goes through the gateway.
_SURFACE_MARKERS = ("sms_inbound", "ussd", "RecordingSmsProvider", "USSD")


def check(context: Context) -> list[Finding]:
    """A DONE frontend layer must point at a USSD/SMS artifact that reaches the contract."""
    findings: list[Finding] = []
    operations = context.operations_for
    # Read the test files once; both membership tests below use the same text.
    texts = {
        path.as_posix(): path.read_text(encoding="utf-8", errors="replace")
        for path in context.frontend_files + context.test_files
    }
    # A USSD tree module is a surface artifact on its own; note which requirement IDs a tree
    # mentions is impossible (IDs are forbidden there), so trees are only consulted for the
    # requirement's own test to exist.
    for row in context.matrix_rows:
        requirement = row["requirement_id"]
        if "L5" not in (row.get("applicable_layers") or ""):
            continue
        if (row.get("frontend_status") or "").strip() != "DONE":
            continue

        if not operations.get(requirement):
            findings.append(
                Finding(
                    check=CHECK,
                    severity=Severity.BLOCKING,
                    requirement=requirement,
                    message="frontend layer is DONE but no contract operation carries it",
                    evidence=f"docs/openapi.yaml:{requirement}",
                )
            )
            continue

        if not _has_surface_artifact(texts, requirement):
            findings.append(
                Finding(
                    check=CHECK,
                    severity=Severity.BLOCKING,
                    requirement=requirement,
                    message="frontend layer is DONE but no USSD/SMS artifact builds it",
                    evidence=f"traceability/MATRIX.csv:{requirement}",
                )
            )
    return findings


def _has_surface_artifact(texts: dict[str, str], requirement: str) -> bool:
    """Whether a USSD/SMS-driving test names the requirement, by ID or by function name."""
    needle_underscore = requirement.replace("-", "_")
    for path, text in texts.items():
        names_it = needle_underscore in text or requirement in text
        if not names_it:
            continue
        drives_surface = any(marker in text for marker in _SURFACE_MARKERS) or "sms" in path.lower()
        if drives_surface:
            return True
    return False
