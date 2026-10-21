"""The scan's orchestration: read the artifacts once, hand them to every check, collect.

Reading is separated from checking so a check is a pure function over plain data and can be
tested against a fixture tree with no filesystem at all. The context below is that plain
data. The scanner walks the repository once, builds the context, runs the checks in a fixed
order, sorts the findings, and returns them.
"""

from __future__ import annotations

import csv
import os
import re
from pathlib import Path

import yaml

from packages.gapscan.checks import CHECKS
from packages.gapscan.config import Context, Finding, Milestone

#: A requirement ID as it is written everywhere outside the ledger's own first column.
REQUIREMENT_ID = re.compile(r"\b(FR|SEC|NFR|PRV)-(\d+[A-Z]?)\b")

#: Directories a walk never descends into. Build output, caches, and virtual environments are
#: not source and a finding about them is noise.
SKIPPED_DIRECTORIES = frozenset(
    {
        ".git",
        "__pycache__",
        ".venv",
        "node_modules",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "htmlcov",
        ".verify-venv",
        "dist",
        "build",
    }
)


def scan(
    *,
    milestone: Milestone,
    matrix: Path,
    openapi: Path,
    repo: Path,
) -> list[Finding]:
    """Read the artifacts under ``repo`` and run every check. Findings are sorted."""
    context = _build_context(milestone=milestone, matrix=matrix, openapi=openapi, repo=repo)
    findings: list[Finding] = []
    for check in CHECKS:
        findings.extend(check(context))
    return sorted(findings, key=lambda finding: finding.sort_key)


def _build_context(*, milestone: Milestone, matrix: Path, openapi: Path, repo: Path) -> Context:
    return Context(
        milestone=milestone,
        matrix_rows=_read_matrix(matrix),
        operations=_read_openapi(openapi),
        test_ids=_read_test_ids(repo),
        test_files=_read_test_files(repo),
        production_files=_read_production_files(repo),
        frontend_files=_read_frontend_files(repo),
        tree_names=_read_tree_names(repo),
    )


def _read_matrix(path: Path) -> list[dict[str, str]]:
    """I1: one row per requirement, keyed by the header the file actually carries."""
    if not path.exists():
        raise FileNotFoundError(f"traceability matrix not found: {path}")
    with path.open(newline="", encoding="utf-8") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _read_openapi(path: Path) -> list[tuple[str, str, list[str]]]:
    """I2: every operation as ``(method, path, x-requirements)``.

    Parameters are not operations and are skipped by the method filter. A path with no
    operations is skipped rather than reported: the contract may carry a path item that only
    holds shared parameters, and that is not drift.
    """
    if not path.exists():
        return []
    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    operations: list[tuple[str, str, list[str]]] = []
    methods = {"get", "post", "put", "patch", "delete", "head", "options"}
    for raw_path, item in (document.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        for method, operation in item.items():
            if method.lower() not in methods or not isinstance(operation, dict):
                continue
            requirements = operation.get("x-requirements") or []
            if isinstance(requirements, str):
                requirements = [requirements]
            operations.append((method.upper(), str(raw_path), [str(item) for item in requirements]))
    return operations


def _read_test_files(repo: Path) -> list[Path]:
    """Every test file, sorted, for the checks that read test source."""
    tests_root = repo / "apps" / "api" / "tests"
    if not tests_root.exists():
        return []
    return sorted(tests_root.rglob("test_*.py"))


def _read_test_ids(repo: Path) -> dict[str, list[str]]:
    """I5: requirement ID -> test files whose function names carry it.

    The ID is read from test *function* names (``def test_FR_601_...``), which is where the
    naming checklist puts it, not from the file name — a file named for three requirements
    would otherwise credit only the first. A file that names a requirement in any function is
    the artifact for that requirement.
    """
    index: dict[str, list[str]] = {}
    pattern = re.compile(r"^\s*(?:async\s+)?def\s+test_([A-Z]+)_(\d+[A-Z]?)", re.MULTILINE)
    for path in _read_test_files(repo):
        text = _read_text(path)
        rel = _relative(path, repo)
        for prefix, number in pattern.findall(text):
            index.setdefault(f"{prefix}-{number}", []).append(rel)
    return index


def _read_production_files(repo: Path) -> list[Path]:
    """I4: every ``.py`` under the production trees, tests and caches excluded.

    ``migrations`` is included here; the check that forbids requirement IDs decides for itself
    that a migration comment is allowed, and the check that hunts planning names wants to see
    the whole tree.
    """
    files: list[Path] = []
    for tree in ("apps/api", "packages"):
        root = repo / tree
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            parts = path.relative_to(repo).parts
            if any(part in SKIPPED_DIRECTORIES for part in parts):
                continue
            if "tests" in parts:
                continue
            files.append(path)
    return files


def _read_frontend_files(repo: Path) -> list[Path]:
    """The Phase 1 frontend artifacts: the USSD trees and the SMS/USSD test files.

    Phase 1 has no mobile app; its user-facing surface is USSD and SMS, whose artifacts are
    the USSD tree modules and the journey tests that drive them.
    """
    files: list[Path] = []
    ussd = repo / "packages" / "ussd"
    if ussd.exists():
        files.extend(sorted(ussd.rglob("*.py")))
    for path in _read_test_files(repo):
        name = path.name.lower()
        if "ussd" in name or "sms" in name or "feature_phone" in name:
            files.append(path)
    return files


def _read_tree_names(repo: Path) -> list[str]:
    """Every file and directory name in the repository walk, for the planning-name check.

    The whole tree is walked — a phase-named directory in ``infra/`` is as forbidden as one in
    ``apps/`` — with build output, caches and virtual environments skipped. Paths are relative
    and POSIX so a finding reads the same on any machine.
    """
    names: list[str] = []
    for root, dirs, files in os.walk(repo):
        dirs[:] = sorted(d for d in dirs if d not in SKIPPED_DIRECTORIES)
        for name in dirs + sorted(files):
            names.append(_relative(Path(root) / name, repo))
    return names


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _relative(path: Path, repo: Path) -> str:
    try:
        return path.relative_to(repo).as_posix()
    except ValueError:
        return path.as_posix()
