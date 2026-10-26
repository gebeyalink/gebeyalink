"""G9 — an in-scope requirement with no test named for it, in any of the three places.

The tool reads test *names*, not coverage. A requirement whose ID appears in a test file name,
a test function name, or a test docstring has been claimed by a test; whether that test passes
is the CI gate's question and this tool says so by setting ``deferred_to_ci`` on every finding
it emits. That split is deliberate: a tool that ran the suite would be a second test runner
with its own environment to keep alive, and the suite is already the source of truth for green.
"""

from __future__ import annotations

from packages.gapscan.config import Context, Finding, Severity

CHECK = "G9"


def check(context: Context) -> list[Finding]:
    """One finding per in-scope requirement with no test named for it in any of the three places."""
    findings: list[Finding] = []
    for requirement in sorted(context.milestone.in_scope):
        if context.test_ids.get(requirement):
            continue
        findings.append(
            Finding(
                check=CHECK,
                severity=Severity.BLOCKING,
                requirement=requirement,
                message="no test named for this requirement in a file name, a function name, "
                "or a docstring (whether it passes is deferred to CI)",
                evidence=f"apps/api/tests:test_{requirement.replace('-', '_')}_*.py",
                deferred_to_ci=True,
            )
        )
    return findings
