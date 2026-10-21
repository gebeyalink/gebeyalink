"""The gap scan's command line: ``python -m packages.gapscan --milestone phase-1``.

The CLI's whole job is to turn arguments into a scan, apply waivers, and translate the result
into an exit code a milestone gate can read. It prints the report to stdout always, and writes
it to ``--json`` as well when asked; the exit code is the part CI reads. Nothing here reads the
environment — the tool's inputs are its arguments, so a scan is reproducible from its command
line alone.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

from packages.gapscan.checks import CHECKS
from packages.gapscan.config import Severity, load_waivers, milestone_or_raise
from packages.gapscan.report import build_report, dumps, exit_code
from packages.gapscan.scanner import scan

#: The defaults describe a scan run from the repository root with nothing to say.
DEFAULT_MATRIX = "traceability/MATRIX.csv"
DEFAULT_OPENAPI = "docs/openapi.yaml"
DEFAULT_REPO = "."
DEFAULT_JSON = "gapscan-report.json"
DEFAULT_FAIL_ON = Severity.BLOCKING

#: The waivers file lives beside the tool, not beside the repository it scans.
WAIVERS_PATH = Path(__file__).with_name("waivers.yaml")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gapscan",
        description="Mechanically scan a milestone for drift, missing work, and leaked names.",
    )
    parser.add_argument("--milestone", required=True, help="the milestone to scan, e.g. phase-1")
    parser.add_argument("--matrix", default=DEFAULT_MATRIX, help="path to the requirement ledger")
    parser.add_argument("--openapi", default=DEFAULT_OPENAPI, help="path to the API contract")
    parser.add_argument("--repo", default=DEFAULT_REPO, help="repository root to scan")
    parser.add_argument("--ci-report", default=None, help="a CI test report (reserved for G7)")
    parser.add_argument("--json", default=DEFAULT_JSON, help="where to write the JSON report")
    parser.add_argument(
        "--fail-on",
        choices=[str(Severity.WARNING), str(Severity.BLOCKING)],
        default=str(DEFAULT_FAIL_ON),
        help="the severity at or above which the exit code is 2",
    )
    parser.add_argument("--waivers", default=str(WAIVERS_PATH), help="path to the waivers file")
    parser.add_argument(
        "--now",
        default=None,
        help="today's date as YYYY-MM-DD, for waiver expiry (defaults to the real date)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the scan, print the report, and return the exit code."""
    args = build_parser().parse_args(argv)
    try:
        milestone = milestone_or_raise(args.milestone)
    except KeyError as error:
        print(str(error), file=sys.stderr)
        return 2

    repo = Path(args.repo)
    matrix = _resolve(args.matrix, repo)
    openapi = _resolve(args.openapi, repo)
    today = dt.date.fromisoformat(args.now) if args.now else dt.date.today()

    findings = scan(milestone=milestone, matrix=matrix, openapi=openapi, repo=repo)
    waivers = load_waivers(Path(args.waivers))
    waived = [
        finding
        for finding in findings
        if any(waiver.covers(finding, today=today) for waiver in waivers)
    ]
    surviving = [finding for finding in findings if finding not in waived]

    report = build_report(
        milestone=milestone.id,
        findings=surviving,
        checks_run=len(CHECKS),
        repo=repo,
        waived=len(waived),
    )
    text = dumps(report)

    json_path = Path(args.json)
    if str(json_path) != "-":
        json_path.write_text(text, encoding="utf-8")
    sys.stdout.write(text)
    return exit_code(surviving, fail_on=Severity(args.fail_on))


def _resolve(path: str, repo: Path) -> Path:
    """A path relative to the repository root unless it is already absolute."""
    candidate = Path(path)
    return candidate if candidate.is_absolute() else repo / candidate


if __name__ == "__main__":  # pragma: no cover - the module runner's entry
    raise SystemExit(main())
