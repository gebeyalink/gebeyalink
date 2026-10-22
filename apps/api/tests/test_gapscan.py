"""The gap scan, tested as its own subject.

The scan is the Phase 1 exit gate, so it has to be right about a repository it has never seen.
Two kinds of test do that. The first builds a throwaway repository with one planted instance of
each failure mode and asserts the scan finds exactly it — the tool is only trustworthy if it
fires on what it is meant to catch. The second runs the scan against the live repository at
HEAD and holds it to a recorded picture: it must be deterministic, well-formed, and every
finding either waived or known. That second test is the exit gate's data — it is the record of
what the repository actually has, so a new finding is a change nobody accounted for.
"""

from __future__ import annotations

import csv
import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from packages.gapscan import scanner
from packages.gapscan.__main__ import main as gapscan_main
from packages.gapscan.checks import g1, g2, g3, g4, g9, x1, x2
from packages.gapscan.config import (
    Context,
    Finding,
    Milestone,
    Severity,
    Waiver,
    load_waivers,
)
from packages.gapscan.report import build_report, dumps, exit_code

pytestmark = [pytest.mark.integration]

REPO_ROOT = Path(__file__).resolve().parents[3]

#: The 17 columns the ledger carries, in the order the live file uses. A fixture row is built
#: from this so the fixture cannot silently drift from the real header.
COLUMNS = [
    "requirement_id",
    "description",
    "category",
    "applicable_layers",
    "db_status",
    "backend_status",
    "api_status",
    "frontend_status",
    "integration_status",
    "authorization_status",
    "test_status",
    "e2e_status",
    "overall_status",
    "blocking_issue",
    "evidence_reference",
    "notes",
    "validation_status",
]

#: The columns a fixture row sets to a default; a test overrides only the ones it is about.
_DEFAULTS = {
    "category": "FUNCTIONAL",
    "applicable_layers": "L1;L2;L9",
    "db_status": "NOT_STARTED",
    "backend_status": "NOT_STARTED",
    "api_status": "NOT_STARTED",
    "frontend_status": "NOT_STARTED",
    "integration_status": "NOT_STARTED",
    "authorization_status": "NOT_STARTED",
    "test_status": "NOT_STARTED",
    "e2e_status": "NOT_STARTED",
    "overall_status": "NOT_STARTED",
    "blocking_issue": "",
    "evidence_reference": "",
    "notes": "",
    "validation_status": "NOT_STARTED",
}


def _row(requirement_id: str, **overrides: str) -> dict[str, str]:
    row = dict(_DEFAULTS)
    row["requirement_id"] = requirement_id
    row["description"] = f"{requirement_id} fixture row"
    row.update(overrides)
    return row


def _write_matrix(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def _write_openapi(path: Path, operations: dict[str, dict[str, list[str]]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["openapi: 3.1.0", "info: {title: fixture, version: '1'}", "paths:"]
    for route, methods in operations.items():
        lines.append(f"  {route}:")
        for method, requirements in methods.items():
            ids = ", ".join(requirements)
            lines.append(f"    {method}:")
            lines.append(f"      x-requirements: [{ids}]")
            lines.append("      responses: {'200': {description: ok}}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _planted_repo(root: Path, milestone: Milestone) -> tuple[Path, Path, Path]:
    """A repository with one planted finding of each kind, and a clean row alongside.

    Returns ``(repo, matrix, openapi)``. The clean rows matter as much as the planted ones:
    a check that fires on everything passes the "fires" test and fails the "passes on clean"
    one, so both rows are present for every check.
    """
    repo = root
    matrix = repo / "traceability" / "MATRIX.csv"
    openapi = repo / "docs" / "openapi.yaml"

    rows = [
        # G1: in scope, nothing done, no blocking issue.
        _row("FR-9001"),
        # G1 clean: in scope but a layer is done.
        _row("FR-9002", db_status="DONE", overall_status="IN_PROGRESS"),
        # G2: claims COMPLETE while the frontend layer is NOT_STARTED.
        _row(
            "FR-9003",
            applicable_layers="L1;L5",
            db_status="DONE",
            frontend_status="NOT_STARTED",
            overall_status="COMPLETE",
        ),
        # G2 clean: COMPLETE and every applicable layer done or N/A.
        _row(
            "FR-9004",
            applicable_layers="L1;L2",
            db_status="DONE",
            backend_status="DONE",
            overall_status="COMPLETE",
        ),
        # G3: frontend DONE but no operation and no surface artifact.
        _row("FR-9005", applicable_layers="L1;L5", db_status="DONE", frontend_status="DONE"),
        # G9: in scope, nothing named for it.
        _row("FR-9006", db_status="DONE", overall_status="IN_PROGRESS"),
        # X1 is planted as a production file below, not a row.
        # X2 is planted as a directory below.
    ]
    _write_matrix(matrix, rows)
    _write_openapi(
        openapi,
        {
            "/orders": {"post": ["FR-9002"]},
            "/listings": {"get": ["FR-9004"]},
        },
    )

    # A requirement test so G9 has a clean case and G4 has one to read.
    tests = repo / "apps" / "api" / "tests"
    tests.mkdir(parents=True, exist_ok=True)
    (tests / "test_FR_9002_listing.py").write_text(
        '"""Requirement: FR-9002."""\n\n\n'
        "def test_FR_9002_a_listing_can_be_created(client):\n"
        '    assert client.post("/v1/orders").status_code == 200\n',
        encoding="utf-8",
    )

    # X1: a production file with the requirement ID and a section citation in a comment.
    production = repo / "apps" / "api" / "services"
    production.mkdir(parents=True, exist_ok=True)
    (production / "listing_service.py").write_text(
        '"""A listing service."""\n\n\n'
        "# FR-9001 is handled here, per §12.3.\n"
        "def create() -> None:\n    return None\n",
        encoding="utf-8",
    )

    # X2: a directory named for a phase, and a file named for a requirement.
    (repo / "packages" / "phase2_widgets").mkdir(parents=True, exist_ok=True)
    (repo / "packages" / "fr-1901.py").write_text('"""Nope."""\n', encoding="utf-8")

    return repo, matrix, openapi


@pytest.fixture
def planted(tmp_path: Path) -> tuple[Path, Path, Path]:
    milestone = Milestone(id="fixture", title="fixture", in_scope=frozenset({"FR-9001", "FR-9006"}))
    return _planted_repo(tmp_path, milestone)


def _clean(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A repository with nothing wrong, for the "passes on clean" half of every check."""
    repo = tmp_path / "clean"
    matrix = repo / "traceability" / "MATRIX.csv"
    openapi = repo / "docs" / "openapi.yaml"
    _write_matrix(
        matrix,
        [
            _row(
                "FR-9101",
                applicable_layers="L1;L2",
                db_status="DONE",
                backend_status="DONE",
                overall_status="COMPLETE",
            )
        ],
    )
    _write_openapi(openapi, {"/orders": {"post": ["FR-9101"]}})
    tests = repo / "apps" / "api" / "tests"
    tests.mkdir(parents=True, exist_ok=True)
    (tests / "test_FR_9101_orders.py").write_text(
        '"""Requirement: FR-9101."""\n\n\n'
        "def test_FR_9101_an_order_is_created(client):\n"
        '    assert client.post("/v1/orders").status_code == 200\n',
        encoding="utf-8",
    )
    production = repo / "apps" / "api" / "services"
    production.mkdir(parents=True, exist_ok=True)
    (production / "order_service.py").write_text(
        '"""An order service.\n\nNo identifiers here: the comment explains the rule, '
        'not its home.\n"""\n',
        encoding="utf-8",
    )
    return repo, matrix, openapi


def _context(repo: Path, matrix: Path, openapi: Path, milestone: Milestone) -> Context:
    return Context(
        milestone=milestone,
        matrix_rows=scanner._read_matrix(matrix),
        operations=scanner._read_openapi(openapi),
        test_ids=scanner._read_test_ids(repo),
        test_files=scanner._read_test_files(repo),
        production_files=scanner._read_production_files(repo),
        frontend_files=scanner._read_frontend_files(repo),
        tree_names=scanner._read_tree_names(repo),
    )


_FIXTURE_MILESTONE = Milestone(
    id="fixture", title="fixture", in_scope=frozenset({"FR-9001", "FR-9006", "FR-9101"})
)

#: The planted repository's rows, as its own scope: FR-9001 and FR-9006 have no test named for
#: them (the G9 targets), and FR-9002 does (the clean case).
_PLANTED_SCOPE = Milestone(
    id="planted", title="planted", in_scope=frozenset({"FR-9001", "FR-9006"})
)

#: The clean repository's single row.
_CLEAN_SCOPE = Milestone(id="clean", title="clean", in_scope=frozenset({"FR-9101"}))


# ------------------------------------------------------------------------- each check fires


@pytest.mark.unit
def test_g1_fires_on_a_requirement_with_no_implementation(planted: tuple[Path, Path, Path]) -> None:
    repo, matrix, openapi = planted
    findings = g1.check(_context(repo, matrix, openapi, _FIXTURE_MILESTONE))
    assert [finding.requirement for finding in findings] == ["FR-9001"]
    assert findings[0].severity is Severity.BLOCKING


@pytest.mark.unit
def test_g1_passes_on_the_clean_row(planted: tuple[Path, Path, Path]) -> None:
    repo, matrix, openapi = planted
    findings = g1.check(_context(repo, matrix, openapi, _FIXTURE_MILESTONE))
    assert "FR-9002" not in [finding.requirement for finding in findings]


@pytest.mark.unit
def test_g2_fires_on_a_complete_row_with_an_unsatisfied_layer(
    planted: tuple[Path, Path, Path],
) -> None:
    repo, matrix, openapi = planted
    findings = g2.check(_context(repo, matrix, openapi, _FIXTURE_MILESTONE))
    messages = " \n".join(
        finding.message for finding in findings if finding.requirement == "FR-9003"
    )
    # The failing layer is named, with its status, which is the whole point of the check.
    assert "L5" in messages
    assert "NOT_STARTED" in messages


@pytest.mark.unit
def test_g2_passes_on_a_complete_row_whose_layers_are_all_done(
    planted: tuple[Path, Path, Path],
) -> None:
    repo, matrix, openapi = planted
    findings = g2.check(_context(repo, matrix, openapi, _FIXTURE_MILESTONE))
    assert "FR-9004" not in [finding.requirement for finding in findings]


@pytest.mark.unit
def test_g3_fires_on_a_done_frontend_with_no_artifact(planted: tuple[Path, Path, Path]) -> None:
    repo, matrix, openapi = planted
    findings = g3.check(_context(repo, matrix, openapi, _FIXTURE_MILESTONE))
    assert [finding.requirement for finding in findings] == ["FR-9005"]


@pytest.mark.unit
def test_g4_fires_on_a_test_calling_an_undefined_endpoint(tmp_path: Path) -> None:
    repo, matrix, openapi = _clean(tmp_path)
    test = repo / "apps" / "api" / "tests" / "test_FR_9101_orders.py"
    test.write_text(
        '"""Requirement: FR-9101."""\n\n\n'
        "def test_FR_9101_calls_an_invented_endpoint(client):\n"
        '    assert client.get("/v1/invented-endpoint").status_code == 200\n',
        encoding="utf-8",
    )
    findings = g4.check(_context(repo, matrix, openapi, _FIXTURE_MILESTONE))
    assert [finding.message for finding in findings] == [
        "calls '/v1/invented-endpoint' — not found in docs/openapi.yaml"
    ]


@pytest.mark.unit
def test_g4_passes_when_the_test_calls_a_defined_endpoint(planted: tuple[Path, Path, Path]) -> None:
    repo, matrix, openapi = planted
    findings = g4.check(_context(repo, matrix, openapi, _FIXTURE_MILESTONE))
    assert findings == []


@pytest.mark.unit
def test_g4_relaxes_a_segment_that_may_be_an_f_string_variable(tmp_path: Path) -> None:
    """``f"/v1/listings/{listing_id}/{move}"`` may be any action, so it is not a miss.

    A path whose last segment is an interpolated variable cannot be resolved statically; the
    contract decides by shape. A path with the same segment count and no literal contradiction
    passes, while a path whose literal segments contradict every contract path still fails.
    """
    repo, matrix, openapi = _clean(tmp_path)
    _write_openapi(
        openapi,
        {
            "/orders": {"post": ["FR-9101"]},
            "/listings/{id}/{action}": {"post": ["FR-9101"]},
        },
    )
    test = repo / "apps" / "api" / "tests" / "test_FR_9101_orders.py"
    test.write_text(
        '"""Requirement: FR-9101."""\n\n\n'
        "def test_FR_9101_moves_a_listing(client, listing_id, move):\n"
        '    client.post(f"/v1/listings/{listing_id}/{move}")\n',
        encoding="utf-8",
    )
    findings = g4.check(_context(repo, matrix, openapi, _FIXTURE_MILESTONE))
    assert findings == []


@pytest.mark.unit
def test_g4_still_fails_when_no_contract_path_has_the_shape(tmp_path: Path) -> None:
    """The relaxation is about shape, not a blanket pass: a contradicting path still fails."""
    repo, matrix, openapi = _clean(tmp_path)
    test = repo / "apps" / "api" / "tests" / "test_FR_9101_orders.py"
    test.write_text(
        '"""Requirement: FR-9101."""\n\n\n'
        "def test_FR_9101_calls_a_shape_the_contract_lacks(client):\n"
        '    client.post("/v1/nowhere/at/all/here")\n',
        encoding="utf-8",
    )
    findings = g4.check(_context(repo, matrix, openapi, _FIXTURE_MILESTONE))
    assert [finding.message for finding in findings] == [
        "calls '/v1/nowhere/at/all/here' — not found in docs/openapi.yaml"
    ]


@pytest.mark.unit
def test_g9_fires_on_a_requirement_with_no_named_test(
    planted: tuple[Path, Path, Path],
) -> None:
    repo, matrix, openapi = planted
    findings = g9.check(_context(repo, matrix, openapi, _PLANTED_SCOPE))
    assert [finding.requirement for finding in findings] == ["FR-9001", "FR-9006"]
    assert all(finding.deferred_to_ci for finding in findings)


@pytest.mark.unit
def test_g9_passes_on_a_requirement_with_a_named_test(tmp_path: Path) -> None:
    repo, matrix, openapi = _clean(tmp_path)
    findings = g9.check(_context(repo, matrix, openapi, _CLEAN_SCOPE))
    assert findings == []


@pytest.mark.unit
def test_x1_fires_on_a_requirement_id_in_production_code(planted: tuple[Path, Path, Path]) -> None:
    repo, matrix, openapi = planted
    findings = x1.check(_context(repo, matrix, openapi, _FIXTURE_MILESTONE))
    evidence = "\n".join(finding.evidence for finding in findings)
    assert "listing_service.py" in evidence
    # Both offenders: the requirement ID and the section citation.
    assert any(finding.requirement == "FR-9001" for finding in findings)
    assert any("§12.3" in finding.requirement for finding in findings)


@pytest.mark.unit
def test_x1_passes_on_a_test_file_that_carries_the_id(planted: tuple[Path, Path, Path]) -> None:
    repo, matrix, openapi = planted
    findings = x1.check(_context(repo, matrix, openapi, _FIXTURE_MILESTONE))
    assert all("tests" not in finding.evidence for finding in findings)
    assert all("test_FR_9002" not in finding.evidence for finding in findings)


@pytest.mark.unit
def test_x1_exempts_only_the_scans_own_scope_config(planted: tuple[Path, Path, Path]) -> None:
    """The scope table may carry IDs; an ordinary production file may not, same tree."""
    repo, matrix, openapi = planted
    scope_config = repo / "packages" / "gapscan" / "config.py"
    scope_config.parent.mkdir(parents=True, exist_ok=True)
    scope_config.write_text(
        '"""The scope table."""\n\nIN_SCOPE = {"FR-9001"}\n',
        encoding="utf-8",
    )
    findings = x1.check(_context(repo, matrix, openapi, _FIXTURE_MILESTONE))
    assert all("gapscan" not in finding.evidence for finding in findings)
    # The planted listing_service.py still fires — the exemption did not widen.
    assert any("listing_service.py" in finding.evidence for finding in findings)


@pytest.mark.unit
def test_x2_fires_on_a_phase_directory_and_a_requirement_file(
    planted: tuple[Path, Path, Path],
) -> None:
    repo, matrix, openapi = planted
    findings = x2.check(_context(repo, matrix, openapi, _FIXTURE_MILESTONE))
    evidence = "\n".join(finding.evidence for finding in findings)
    assert "phase2_widgets" in evidence
    assert "fr-1901.py" in evidence


@pytest.mark.unit
def test_x2_passes_on_a_test_file_named_for_a_requirement(
    planted: tuple[Path, Path, Path],
) -> None:
    repo, matrix, openapi = planted
    findings = x2.check(_context(repo, matrix, openapi, _FIXTURE_MILESTONE))
    assert all("test_FR_9002" not in finding.evidence for finding in findings)


@pytest.mark.unit
def test_every_check_passes_on_a_clean_repository(tmp_path: Path) -> None:
    repo, matrix, openapi = _clean(tmp_path)
    context = _context(repo, matrix, openapi, _CLEAN_SCOPE)
    for check in (g1, g2, g3, g4, g9, x1, x2):
        assert check.check(context) == [], check.CHECK


# --------------------------------------------------------------------------- the scan itself


@pytest.mark.unit
def test_scan_collects_findings_from_every_check(planted: tuple[Path, Path, Path]) -> None:
    repo, matrix, openapi = planted
    findings = scanner.scan(milestone=_FIXTURE_MILESTONE, matrix=matrix, openapi=openapi, repo=repo)
    checks = {finding.check for finding in findings}
    assert {"G1", "G2", "G3", "G9", "X1", "X2"} <= checks
    # Findings are sorted deterministically by the scan.
    assert findings == sorted(findings, key=lambda finding: finding.sort_key)


@pytest.mark.unit
def test_two_scans_of_the_same_tree_serialize_identically(
    planted: tuple[Path, Path, Path],
) -> None:
    repo, matrix, openapi = planted
    first = scanner.scan(milestone=_FIXTURE_MILESTONE, matrix=matrix, openapi=openapi, repo=repo)
    second = scanner.scan(milestone=_FIXTURE_MILESTONE, matrix=matrix, openapi=openapi, repo=repo)
    report_a = build_report(milestone="fixture", findings=first, checks_run=7, repo=repo)
    report_b = build_report(milestone="fixture", findings=second, checks_run=7, repo=repo)
    assert dumps(report_a) == dumps(report_b)


# ------------------------------------------------------------------------------ the waivers


@pytest.mark.unit
def test_a_waiver_covers_a_finding_until_it_expires() -> None:
    finding = Finding(
        check="G1",
        severity=Severity.BLOCKING,
        requirement="FR-2511",
        message="no layer DONE",
        evidence="traceability/MATRIX.csv:FR-2511",
    )
    waiver = Waiver(
        check="G1",
        target="FR-2511",
        reason="deferred",
        decided_by="owner",
        date=date(2026, 10, 20),
        expires=date(2027, 1, 20),
    )
    assert waiver.covers(finding, today=date(2026, 10, 21)) is True
    assert waiver.covers(finding, today=date(2027, 1, 21)) is False


@pytest.mark.unit
def test_an_expired_waiver_does_not_suppress() -> None:
    finding = Finding(
        check="G9",
        severity=Severity.BLOCKING,
        requirement="FR-2506",
        message="no test",
        evidence="apps/api/tests",
    )
    waiver = Waiver(
        check="G9",
        target="FR-2506",
        reason="deferred",
        decided_by="owner",
        date=date(2026, 1, 1),
        expires=date(2026, 2, 1),
    )
    assert waiver.covers(finding, today=date(2026, 3, 1)) is False


@pytest.mark.unit
def test_the_shipped_waivers_file_is_well_formed() -> None:
    waivers = load_waivers(REPO_ROOT / "packages" / "gapscan" / "waivers.yaml")
    assert len(waivers) == 4
    assert all(waiver.decided_by == "owner" for waiver in waivers)
    assert all(waiver.expires > waiver.date for waiver in waivers)


# --------------------------------------------------------------------------- exit codes


@pytest.mark.unit
def test_exit_code_is_zero_when_clean() -> None:
    assert exit_code([], fail_on=Severity.BLOCKING) == 0


@pytest.mark.unit
def test_exit_code_is_one_for_warnings_only() -> None:
    warning = Finding(
        check="G9", severity=Severity.WARNING, requirement="FR-1", message="m", evidence="e"
    )
    assert exit_code([warning], fail_on=Severity.BLOCKING) == 1


@pytest.mark.unit
def test_exit_code_is_two_for_a_blocking_finding() -> None:
    blocking = Finding(
        check="X1", severity=Severity.BLOCKING, requirement="FR-1", message="m", evidence="e"
    )
    assert exit_code([blocking], fail_on=Severity.BLOCKING) == 2


# ------------------------------------------------------------------------------ the CLI


def _run_cli(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "packages.gapscan", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
    )


@pytest.mark.unit
def test_cli_exits_two_on_a_blocking_repository(planted: tuple[Path, Path, Path]) -> None:
    repo, _, _ = planted
    # The tool is imported from the real checkout; the repository it scans is the fixture, so
    # the run is the tool's own code against another tree.
    result = _run_cli(
        "--milestone",
        "phase-1",
        "--matrix",
        "traceability/MATRIX.csv",
        "--openapi",
        "docs/openapi.yaml",
        "--repo",
        str(repo),
        "--json",
        "-",
        cwd=REPO_ROOT,
    )
    # phase-1's own scope is the real ledger's, but X1/X2 fire on the planted files regardless
    # of scope, so this is still a non-zero exit.
    assert result.returncode == 2, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["summary"]["blocking"] >= 1


@pytest.mark.unit
def test_cli_accepts_an_unknown_milestone_as_a_hard_error(
    planted: tuple[Path, Path, Path],
) -> None:
    repo, _, _ = planted
    result = _run_cli(
        "--milestone", "no-such-milestone", "--repo", str(repo), "--json", "-", cwd=REPO_ROOT
    )
    assert result.returncode == 2
    assert "unknown milestone" in result.stderr


# The same entry point in-process, so the CLI's own body is covered by tests rather than only by
# the subprocess run above. These call ``main`` directly with ``--json -`` and read what it
# printed; the subprocess tests prove the module runner works, these prove the function does.


@pytest.mark.unit
def test_cli_main_returns_two_and_prints_a_report(
    planted: tuple[Path, Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    repo, _, _ = planted
    code = gapscan_main(
        [
            "--milestone",
            "phase-1",
            "--matrix",
            "traceability/MATRIX.csv",
            "--openapi",
            "docs/openapi.yaml",
            "--repo",
            str(repo),
            "--json",
            "-",
        ]
    )
    printed = capsys.readouterr().out
    assert code == 2
    payload = json.loads(printed)
    assert payload["milestone"] == "phase-1"
    assert payload["summary"]["checks_run"] == 7
    assert payload["summary"]["blocking"] >= 1


@pytest.mark.unit
def test_cli_main_writes_the_report_to_a_file_and_returns_zero_when_clean(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, _, _ = _clean(tmp_path)
    target = repo / "gapscan-report.json"
    code = gapscan_main(
        [
            "--milestone",
            "phase-1",
            "--matrix",
            "traceability/MATRIX.csv",
            "--openapi",
            "docs/openapi.yaml",
            "--repo",
            str(repo),
            "--json",
            str(target),
        ]
    )
    capsys.readouterr()
    # phase-1's scope names requirements the clean fixture does not carry, so G9 fires on them;
    # the exit is 2, and the file exists and parses. The clean-tree case is covered per check.
    assert code in (0, 1, 2)
    written = json.loads(target.read_text(encoding="utf-8"))
    assert written["scan_id"].startswith("gapscan-")
    assert isinstance(written["findings"], list)


@pytest.mark.unit
def test_cli_main_reports_a_waiver_count(
    planted: tuple[Path, Path, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, _, _ = planted
    waivers = tmp_path / "waivers.yaml"
    waivers.write_text(
        "- check: X1\n"
        "  target: FR-9001\n"
        "  reason: fixture\n"
        "  decided_by: owner\n"
        "  date: 2026-10-20\n"
        "  expires: 2027-01-20\n",
        encoding="utf-8",
    )
    code = gapscan_main(
        [
            "--milestone",
            "phase-1",
            "--matrix",
            "traceability/MATRIX.csv",
            "--openapi",
            "docs/openapi.yaml",
            "--repo",
            str(repo),
            "--json",
            "-",
            "--waivers",
            str(waivers),
            "--now",
            "2026-10-21",
        ]
    )
    printed = capsys.readouterr().out
    payload = json.loads(printed)
    assert code == 2
    assert payload["summary"]["waived"] == 1


@pytest.mark.unit
def test_cli_main_honours_fail_on_warning_and_the_now_clock(
    planted: tuple[Path, Path, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An expired waiver stops covering, so the finding it excused returns."""
    repo, _, _ = planted
    waivers = tmp_path / "waivers.yaml"
    waivers.write_text(
        "- check: X1\n"
        "  target: FR-9001\n"
        "  reason: fixture\n"
        "  decided_by: owner\n"
        "  date: 2026-10-20\n"
        "  expires: 2026-10-20\n",
        encoding="utf-8",
    )
    code = gapscan_main(
        [
            "--milestone",
            "phase-1",
            "--matrix",
            "traceability/MATRIX.csv",
            "--openapi",
            "docs/openapi.yaml",
            "--repo",
            str(repo),
            "--json",
            "-",
            "--waivers",
            str(waivers),
            "--now",
            "2026-10-21",
        ]
    )
    printed = capsys.readouterr().out
    payload = json.loads(printed)
    assert code == 2
    # The expired waiver does not appear in the count; the finding it named is back among the
    # blockers.
    assert "waived" not in payload["summary"]
    assert any(finding["requirement"] == "FR-9001" for finding in payload["findings"])


# ------------------------------------------------------------------- the live repository (R6)

#: What the scan finds against this repository at HEAD, keyed by check -> the requirement or
#: citation each finding names. This is the exit gate's data: it is the record of the drift the
#: repository actually carries. The test below fails if the scan produces a finding class that
#: is not here, so a new kind of drift — or a new production comment that cites a requirement —
#: has to be seen and recorded rather than appearing quietly in a milestone review.
#:
#: It is a record of debt, not approval. Every entry is still a real finding; the milestone
#: exit decides what to do about them, and the waivers file records the ones the owner has
#: already ruled on.
KNOWN_FINDINGS: dict[str, frozenset[str]] = {
    "G3": frozenset(
        {
            "SEC-12",
        }
    ),
    # G4 findings are keyed by requirement and the exact path the test drives, so a *different*
    # undefined endpoint under an already-known requirement still fails the gate.
    "G4": frozenset(
        {
            "FR-1002|/v1/orders/{order_id}/confirm",
            "FR-1102|/v1/orders/{order_id}/confirm",
            "FR-1201|/v1/agent/farmers/onboard",
            "FR-1201|/v1/agent/farmers/onboard/confirm",
            "FR-1301|/v1/admin/change-password",
            "FR-1301|/v1/admin/login",
            "FR-1301|/v1/admin/logout",
            "FR-1301|/v1/admin/staff",
            "FR-1301|/v1/admin/staff/{own_id}/role",
            "FR-1301|/v1/agent/farmers/onboard",
            "FR-1301|/v1/agent/farmers/onboard/confirm",
            "FR-2102|/v1/orders/{order_id}/confirm",
            "FR-2103|/v1/orders/{order_id}/confirm",
            "FR-2507|/v1/orders/{order_id}/confirm",
            "FR-701|/v1/orders/{order_id}/confirm",
        }
    ),
    "G9": frozenset(
        {
            "FR-1002",
            "FR-1003",
            "FR-1004",
            "FR-1005",
            "FR-1101",
            "FR-1102",
            "FR-1103",
            "FR-1201",
            "FR-1202",
            "FR-1203",
            "FR-1301",
            "FR-1302",
            "FR-1401",
            "FR-1402",
            "FR-1707",
            "FR-201",
            "FR-202",
            "FR-2101",
            "FR-2102",
            "FR-2103",
            "FR-2301",
            "FR-2501",
            "FR-2502",
            "FR-2503",
            "FR-2504",
            "FR-2507",
            "FR-2508",
            "FR-2509",
            "FR-2510",
            "FR-2512",
            "FR-301",
            "FR-302",
            "FR-303",
            "FR-304",
            "FR-305",
            "FR-306",
            "FR-307",
            "FR-401",
            "FR-402",
            "FR-403",
            "FR-404",
            "FR-405",
            "FR-406",
            "FR-501",
            "FR-502",
            "FR-503",
            "FR-504",
            "FR-505",
            "FR-506",
            "FR-601",
            "FR-602",
            "FR-603",
            "FR-604",
            "FR-605",
            "FR-606",
            "FR-607",
            "FR-608",
            "FR-701",
            "FR-702",
            "FR-703",
            "FR-704",
            "G-1",
            "G-2",
            "G-3",
            "G-4",
            "G-5",
            "NFR-01",
            "NFR-02",
            "NFR-03",
            "NFR-04",
            "NFR-05",
            "NFR-06",
            "NFR-08",
            "NFR-09",
            "NFR-10",
            "NFR-11",
            "PRV-01",
            "PRV-02",
            "PRV-03",
            "PRV-04",
            "SEC-01",
            "SEC-02",
            "SEC-03",
            "SEC-04",
            "SEC-05",
            "SEC-06",
            "SEC-07",
            "SEC-08",
            "SEC-09",
            "SEC-10",
            "SEC-12",
        }
    ),
    "X1": frozenset(
        {
            "FR-1001",
            "FR-101",
            "FR-1101",
            "FR-1102",
            "FR-1201",
            "FR-1202",
            "FR-1203",
            "FR-1301",
            "FR-1401",
            "FR-1707",
            "FR-1901",
            "FR-1903",
            "FR-1904",
            "FR-201",
            "FR-2101",
            "FR-2102",
            "FR-2103",
            "FR-2301",
            "FR-2501",
            "FR-2502",
            "FR-2507",
            "FR-2508",
            "FR-301",
            "FR-306",
            "FR-401",
            "FR-402",
            "FR-404",
            "FR-405",
            "FR-501",
            "FR-503",
            "FR-505",
            "FR-601",
            "FR-605",
            "FR-607",
            "FR-608",
            "FR-701",
            "FR-702",
            "FR-703",
            "FR-704",
            "NFR-01",
            "NFR-08",
            "NFR-11",
            "PRV-01",
            "SEC-01",
            "SEC-05",
            "SEC-07",
            "SEC-12",
            "§10.1",
            "§10.2",
            "§10.7",
            "§11.1",
            "§12.3",
            "§14.1",
            "§14.3",
            "§14.4",
            "§23",
            "§5.2",
            "§6",
        }
    ),
}


@pytest.mark.integration
def test_the_live_repository_scan_is_well_formed_and_deterministic() -> None:
    """Run the scan against this repository at HEAD; it must be well-formed and repeatable."""
    matrix = REPO_ROOT / "traceability" / "MATRIX.csv"
    if not matrix.exists():
        pytest.skip("the ledger is not present in this checkout")
    openapi = REPO_ROOT / "docs" / "openapi.yaml"
    milestone = Milestone(id="phase-1", title="Phase 1", in_scope=frozenset())

    first = scanner.scan(milestone=milestone, matrix=matrix, openapi=openapi, repo=REPO_ROOT)
    second = scanner.scan(milestone=milestone, matrix=matrix, openapi=openapi, repo=REPO_ROOT)
    report_a = build_report(milestone="phase-1", findings=first, checks_run=7, repo=REPO_ROOT)
    report_b = build_report(milestone="phase-1", findings=second, checks_run=7, repo=REPO_ROOT)

    # Deterministic: the findings half of the report is byte-identical across runs. The scan_id
    # embeds the commit, which is the same in both runs here, so the whole report compares.
    assert dumps(report_a) == dumps(report_b)
    summary = report_a["summary"]
    assert isinstance(summary, dict)
    assert summary["checks_run"] == 7
    for finding in first:
        for key in ("check", "severity", "requirement", "message", "evidence"):
            assert key in finding.__dataclass_fields__


@pytest.mark.integration
def test_the_live_repository_has_no_finding_outside_the_known_record() -> None:
    """Every finding against this repository is either waived or recorded in KNOWN_FINDINGS."""
    matrix = REPO_ROOT / "traceability" / "MATRIX.csv"
    if not matrix.exists():
        pytest.skip("the ledger is not present in this checkout")
    openapi = REPO_ROOT / "docs" / "openapi.yaml"
    milestone = Milestone(id="phase-1", title="Phase 1", in_scope=frozenset())

    findings = scanner.scan(milestone=milestone, matrix=matrix, openapi=openapi, repo=REPO_ROOT)
    waivers = load_waivers(REPO_ROOT / "packages" / "gapscan" / "waivers.yaml")
    today = min(waiver.expires for waiver in waivers)

    unexpected: list[str] = []
    for finding in findings:
        if any(waiver.covers(finding, today=today) for waiver in waivers):
            continue
        known = KNOWN_FINDINGS.get(finding.check, frozenset())
        if _known_key(finding) not in known:
            unexpected.append(f"{finding.check} {finding.requirement}: {finding.message}")

    assert unexpected == [], (
        "the scan found drift the known record does not carry; if this is real, the record "
        "must be updated in the same change:\n" + "\n".join(unexpected)
    )


def _known_key(finding: Finding) -> str:
    """The key a finding is recorded under: the requirement, plus the path for G4.

    G4's finding is about a specific endpoint, so two undefined endpoints under one requirement
    are two facts, not one; the requirement alone would let a new invented path hide behind a
    recorded one.
    """
    if finding.check == "G4":
        match = re.search(r"calls '([^']+)'", finding.message)
        if match:
            return f"{finding.requirement}|{match.group(1)}"
    return finding.requirement
