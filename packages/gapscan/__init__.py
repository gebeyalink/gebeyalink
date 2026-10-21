"""The gap scan: a mechanical check that a milestone is not declared while drifting.

The tool reads the four artifacts a claim of "done" can be tested against — the requirement
ledger, the published API contract, the test names, and the production tree — and reports
every requirement that has no implementation, a layer that is not what its overall status
claims, a contract operation nothing exercises, or a planning name that leaked into the code.
It never runs the tests; whether a named test passes is the CI gate's question, and the tool
says so where it matters.

Its own output is its interface: a JSON report on stdout or at ``--json``, and an exit code a
milestone gate can read — 0 clean, 1 non-blocking findings only, 2 at least one blocking
finding.
"""

from packages.gapscan.config import Finding, Severity
from packages.gapscan.scanner import scan

__all__ = ["Finding", "Severity", "scan"]
