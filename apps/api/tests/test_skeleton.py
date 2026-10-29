"""Skeleton smoke test — asserts the SPEC §19 repository layout exists.

CI scaffolding, not feature code. Purpose: keep the Phase 0 pipeline honestly green
while there is no application source, and fail loudly the moment someone deletes or
renames a directory the spec pins.

This test asserts *structure only*. It asserts nothing about product behaviour. From
the first real vertical slice (SPEC §23 Phase 0: FR-101..FR-107 auth/OTP) onward, every
gate in `.github/workflows/ci.yml` must pass for real — this file does not relax them.

SPEC §19 — Repository Structure & Code Standards (verbatim tree):
    apps/api (routers, services, repositories, models, alembic/, tests/)
    apps/mobile, apps/admin
    packages/shared, packages/ussd
    infra (terraform/, monitoring/)
    .github/workflows/
SPEC §22 additionally requires infra/runbooks/ with five named runbooks.
"""

from pathlib import Path

import pytest

# Marked `unit` so `pytest -m unit` in ci.yml actually collects it. Without a marker the
# unit job would select zero tests and pytest would exit 5 ("no tests collected") = red CI.
pytestmark = pytest.mark.unit

REPO_ROOT: Path = Path(__file__).resolve().parents[3]

# Directories pinned by SPEC §19, plus infra/runbooks/ (SPEC §22).
SPEC_SKELETON_DIRS: tuple[str, ...] = (
    "apps",
    "apps/api",
    "apps/api/routers",
    "apps/api/services",
    "apps/api/repositories",
    "apps/api/models",
    "apps/api/alembic",
    "apps/api/tests",
    "apps/api/tests/e2e",
    "apps/mobile",
    "apps/admin",
    "packages",
    "packages/shared",
    "packages/ussd",
    "infra",
    "infra/terraform",
    "infra/monitoring",
    "infra/runbooks",
    ".github",
    ".github/workflows",
)

# SPEC §22 names exactly these five runbooks. PRV-03 requires data-breach.md in addition.
SPEC_RUNBOOKS: tuple[str, ...] = (
    "escrow-mismatch.md",
    "payment-outage-failover.md",
    "sms-backlog.md",
    "ussd-timeout-spike.md",
    "restore-drill.md",
    "data-breach.md",
)


@pytest.mark.parametrize("relative_path", SPEC_SKELETON_DIRS)
def test_skeleton_directory_exists(relative_path: str) -> None:
    """Every SPEC §19 / §22 directory is present."""
    assert (REPO_ROOT / relative_path).is_dir(), f"missing directory: {relative_path}"


@pytest.mark.parametrize("runbook", SPEC_RUNBOOKS)
def test_runbook_present(runbook: str) -> None:
    """Each SPEC §22 runbook (plus PRV-03 data-breach.md) exists and is a non-empty file."""
    path: Path = REPO_ROOT / "infra" / "runbooks" / runbook
    assert path.is_file(), f"missing runbook: {runbook}"
    assert path.read_text(encoding="utf-8").strip(), f"empty runbook: {runbook}"


def test_repo_root_is_resolved_correctly() -> None:
    """Guard the parents[] arithmetic above — a wrong REPO_ROOT makes every other test vacuous.

    Deliberately does NOT assert the repo directory is named "GebeyaLink": a clone may
    live in any directory, and this test must not depend on the checkout's name.
    It also does NOT assert SPEC.md exists — the documentation set is gitignored, so a
    CI clone legitimately has no SPEC.md.
    """
    assert REPO_ROOT.is_dir()
    assert (REPO_ROOT / "pyproject.toml").is_file()
    assert (REPO_ROOT / ".gitignore").is_file()


def test_no_unexpected_runbooks() -> None:
    """SPEC §22 fixes the runbook set. An extra file is drift, not an improvement."""
    runbooks_dir: Path = REPO_ROOT / "infra" / "runbooks"
    actual: set[str] = {p.name for p in runbooks_dir.glob("*.md")}
    assert actual == set(SPEC_RUNBOOKS), f"unexpected runbook set: {sorted(actual)}"
