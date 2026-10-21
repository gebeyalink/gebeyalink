"""The scan's report: the JSON a gate reads and a person diffs.

The shape is the spec's: an id, the milestone, a summary, and the findings. The id embeds the
commit the scan ran against, so a report found later says what it was a report *about*; the
findings themselves never carry that id, which is what keeps two runs over the same tree
byte-identical.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from packages.gapscan.config import Finding, Severity

#: A severity at or above this one causes a non-zero exit. ``blocking`` is the default gate.
SEVERITY_RANK = {Severity.WARNING: 1, Severity.BLOCKING: 2}


def scan_id(repo: Path) -> str:
    """``gapscan-<git-sha>``, or ``gapscan-local`` outside a git work tree.

    The short sha is read from the repository the scan was pointed at, not from the tool's own
    location, so a report against another checkout is labelled with that checkout's commit.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "gapscan-local"
    sha = result.stdout.strip()
    return f"gapscan-{sha}" if sha else "gapscan-local"


def build_report(
    *,
    milestone: str,
    findings: list[Finding],
    checks_run: int,
    repo: Path,
    waived: int = 0,
) -> dict[str, object]:
    """The report as a plain dict, findings in the order they were given (already sorted).

    ``waived`` is the count of findings a waiver covered. It rides in the summary rather than
    disappearing, so a reader can see that the scan found more than it failed on and that the
    difference is a recorded decision, not an oversight.
    """
    blocking = sum(1 for finding in findings if finding.severity is Severity.BLOCKING)
    summary: dict[str, object] = {
        "checks_run": checks_run,
        "findings": len(findings),
        "blocking": blocking,
    }
    if waived:
        summary["waived"] = waived
    return {
        "scan_id": scan_id(repo),
        "milestone": milestone,
        "summary": summary,
        "findings": [_finding_json(finding) for finding in findings],
    }


def dumps(report: dict[str, object]) -> str:
    """Stable JSON: sorted keys, one finding per line preserved by the indent, trailing newline."""
    return json.dumps(report, indent=2, ensure_ascii=False, sort_keys=False) + "\n"


def exit_code(findings: list[Finding], *, fail_on: Severity) -> int:
    """0 clean; 1 non-blocking findings only; 2 at least one finding at ``fail_on`` or above."""
    if not findings:
        return 0
    threshold = SEVERITY_RANK[fail_on]
    if any(SEVERITY_RANK[finding.severity] >= threshold for finding in findings):
        return 2
    return 1


def _finding_json(finding: Finding) -> dict[str, object]:
    return {
        "check": finding.check,
        "severity": str(finding.severity),
        "requirement": finding.requirement,
        "message": finding.message,
        "evidence": finding.evidence,
        "deferred_to_ci": finding.deferred_to_ci,
    }
