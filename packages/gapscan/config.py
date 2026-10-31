"""The scan's fixed vocabulary: severities, findings, waivers, and the milestone scope.

Everything here is data the checks share. Nothing here reads a file except
:func:`load_waivers`, which is the one place a waiver file is interpreted; a check sees only
:class:`Waiver` objects and asks whether one covers a finding that has already been found.

The milestone scope is deliberately an explicit list of requirement IDs rather than a
pattern. A pattern ("everything that is not money") drifts silently the moment a requirement
is renumbered or a new family is added; a list has to be edited, and an edit is reviewable in
a diff. The list below is SPEC §23's Phase 1 ("Pilot MVP") — listings, search, the price
board, notifications, agent mode, and admin basics, with no live payments — read against the
requirements the ledger actually carries.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

import yaml


class Severity(StrEnum):
    """How hard a finding stops a milestone."""

    BLOCKING = "blocking"
    WARNING = "warning"


@dataclass(frozen=True, order=True)
class Finding:
    """One thing the scan found, and where.

    The field order is the sort order, so a run's findings are grouped by check and then by
    the requirement or path they name — two runs over the same tree produce the same bytes.
    ``scan_id`` is not part of a finding; it rides on the report, which is what keeps a run's
    findings diffable across commits.

    ``deferred_to_ci`` marks a finding whose resolution is a question only a test run can
    answer — a named test existing is not the same as its passing, and this tool does not run
    tests. It is set on the G9 findings and serialized into the report so a reader knows the
    gate handed that part to CI rather than silently declaring it green.
    """

    check: str
    severity: Severity
    requirement: str
    message: str
    evidence: str
    deferred_to_ci: bool = field(default=False, compare=False)

    @property
    def sort_key(self) -> tuple[str, str, str, str, str]:
        return (self.check, self.requirement, self.message, self.evidence, self.severity)


@dataclass(frozen=True)
class Waiver:
    """A named decision to let a specific finding stand until a date.

    A waiver is not a silencer: it names the check and the target it covers, says who decided
    and when, and carries an expiry. Past the expiry it stops covering, which is the whole
    mechanism — "we will fix it later" cannot stay unsaid forever.
    """

    check: str
    target: str
    reason: str
    decided_by: str
    date: dt.date
    expires: dt.date

    def covers(self, finding: Finding, *, today: dt.date) -> bool:
        """Whether this waiver excuses ``finding`` as of ``today``.

        Covers nothing once expired. The target matches either the requirement the finding
        names or a substring of its message/evidence, so a waiver can be written against a
        requirement ID or against the surface a finding mentions.
        """
        if today > self.expires:
            return False
        if self.check != finding.check:
            return False
        if self.target == finding.requirement:
            return True
        haystack = f"{finding.requirement} {finding.message} {finding.evidence}"
        return self.target in haystack


@dataclass(frozen=True)
class Milestone:
    """A milestone and the requirement IDs the exit gate is responsible for."""

    id: str
    title: str
    in_scope: frozenset[str] = field(default_factory=frozenset)


@dataclass
class Context:
    """Everything the checks read, parsed once.

    Reading and checking are separate so a check is a pure function over plain data and can be
    tested against a fixture tree with no filesystem at all. The ledger is a list of rows keyed
    by the CSV header, so a check names a column rather than a position and a reordered column
    cannot silently shift a status. ``operations`` is a list of ``(method, path, ids)`` triples
    pulled from the contract's paths.
    """

    milestone: Milestone
    matrix_rows: list[dict[str, str]] = field(default_factory=list)
    #: ``(METHOD, path, requirement_ids)`` for every operation in the contract.
    operations: list[tuple[str, str, list[str]]] = field(default_factory=list)
    #: requirement ID -> the test files whose function names carry it.
    test_ids: dict[str, list[str]] = field(default_factory=dict)
    #: Every test file under ``apps/api/tests/``, for the checks that read test source.
    test_files: list[Path] = field(default_factory=list)
    #: Production ``.py`` files (tests and caches excluded) for the text and name checks.
    production_files: list[Path] = field(default_factory=list)
    #: USSD/SMS artifacts — the USSD trees and the surface-driving tests — for G3.
    frontend_files: list[Path] = field(default_factory=list)
    #: Every file and directory name in the repository walk, relative POSIX paths, for X2.
    tree_names: list[str] = field(default_factory=list)
    #: Path of the openapi contract actually read, or "absent" if none was found.
    openapi_source: str = "absent"

    @property
    def matrix_by_id(self) -> dict[str, dict[str, str]]:
        return {row["requirement_id"]: row for row in self.matrix_rows}

    @property
    def operations_for(self) -> dict[str, list[tuple[str, str]]]:
        """requirement ID -> the operations that carry it, via ``x-requirements``."""
        index: dict[str, list[tuple[str, str]]] = {}
        for method, path, ids in self.operations:
            for requirement in ids:
                index.setdefault(requirement, []).append((method, path))
        return index


#: The Phase 1 pilot's scope, as requirement IDs. Read from SPEC §23 ("1 — Pilot MVP:
#: Listings (app+USSD+SMS), search, price board, notifications, agent mode, admin basics. No
#: live payments") against the ledger's own rows. Money movement (escrow, payouts, webhooks,
#: recovery, disputes, ratings) is Phase 2 and is intentionally absent; so are the GA-only
#: families. When SPEC §23's pilot scope changes, this list changes with it.
PHASE_1_IN_SCOPE = frozenset(
    {
        # Registration, OTP sign-in, sessions, language, terms.
        "FR-101",
        "FR-102",
        "FR-103",
        "FR-104",
        "FR-105",
        "FR-106",
        "FR-107",
        # Profiles and location.
        "FR-201",
        "FR-202",
        "FR-203",
        # Listings and their lifecycle.
        "FR-301",
        "FR-302",
        "FR-303",
        "FR-304",
        "FR-305",
        "FR-306",
        "FR-307",
        # Offline-first drafting and resume (the app is a Phase 1 surface).
        "FR-2501",
        "FR-2510",
        # Search and the price board.
        "FR-401",
        "FR-402",
        "FR-403",
        "FR-404",
        "FR-405",
        "FR-406",
        # Offers and negotiation.
        "FR-501",
        "FR-502",
        "FR-503",
        "FR-504",
        "FR-505",
        "FR-506",
        # COD orders (the pilot's only payment mode) and their state machine.
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
        "FR-2101",
        "FR-2102",
        "FR-2103",
        # Notifications: channels, the mandatory SMS events, rate limits, quiet hours, budgets.
        "FR-1001",
        "FR-1002",
        "FR-1003",
        "FR-1004",
        "FR-1005",
        # USSD and SMS parity — the pilot's reach story.
        "FR-1101",
        "FR-1102",
        "FR-1103",
        # Agent mode and the agent's boundary.
        "FR-1201",
        "FR-1202",
        "FR-1203",
        "FR-1707",
        # Admin basics: the queues that gate the pilot.
        "FR-1301",
        "FR-1302",
        # Analytics events the pilot's NPS and listing targets rest on.
        "FR-1401",
        "FR-1402",
        # Regional operations the pilot woredas need.
        "FR-2502",
        "FR-2503",
        "FR-2504",
        "FR-2507",
        "FR-2508",
        "FR-2509",
        "FR-2512",
        # Client support metadata.
        "FR-2301",
        # Security and privacy that the pilot cannot launch without.
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
        "PRV-01",
        "PRV-02",
        "PRV-03",
        "PRV-04",
        # Non-functional targets that hold at pilot scale.
        "NFR-01",
        "NFR-02",
        "NFR-03",
        "NFR-04",
        "NFR-05",
        "NFR-06",
        "NFR-07",
        "NFR-08",
        "NFR-09",
        "NFR-10",
        "NFR-11",
        # The parity goal the pilot is measured against.
        "G-1",
        "G-2",
        "G-3",
        "G-4",
        "G-5",
    }
)

#: The milestones this tool knows. One today; a new one is a new entry, not a new tool.
MILESTONES: dict[str, Milestone] = {
    "phase-1": Milestone(
        id="phase-1",
        title="Phase 1 — Pilot MVP exit",
        in_scope=PHASE_1_IN_SCOPE,
    ),
}


def milestone_or_raise(milestone_id: str) -> Milestone:
    """The milestone, or a clear error naming the ones that exist."""
    if milestone_id not in MILESTONES:
        known = ", ".join(sorted(MILESTONES))
        raise KeyError(f"unknown milestone {milestone_id!r}; known milestones: {known}")
    return MILESTONES[milestone_id]


def load_waivers(path: Path) -> tuple[Waiver, ...]:
    """Read the waivers file. A missing file is no waivers, not an error."""
    if not path.exists():
        return ()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    waivers: list[Waiver] = []
    for entry in raw:
        waivers.append(
            Waiver(
                check=str(entry["check"]),
                target=str(entry["target"]),
                reason=str(entry["reason"]),
                decided_by=str(entry["decided_by"]),
                date=_as_date(entry["date"]),
                expires=_as_date(entry["expires"]),
            )
        )
    return tuple(waivers)


def _as_date(value: object) -> dt.date:
    """A date from either a ``date`` or an ISO-8601 string, so YAML's own inference is fine."""
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value))
