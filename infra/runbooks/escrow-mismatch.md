# Runbook: escrow-mismatch.md

> **Status:** STUB — outline only. No procedure recorded yet.
> Sections below are the required headings per SPEC §22 ("Every page alert links its runbook").
> **Do not treat any step here as real.** Steps are filled in as the ledger/reconciliation
> subsystem lands (Phase 2 — Money, SPEC §23). This document creates no requirements.

## 1. Purpose
_Not yet written. Intended scope: operator response when the escrow/ledger reconciliation
reports any mismatch between the escrow balance and its ledger journals._

## 2. Alerts that trigger this runbook
SPEC §22 alert names (exact, from the spec):
- `escrow_reconciliation_mismatch_total` — page on any occurrence (threshold: 1, i.e. any)

Severity: **page**. This is the only §22 alert with a zero tolerance threshold — a single
mismatch is a page, not a dashboard annotation.

## 3. Who gets paged
_Not yet decided — pending confirmation of the on-call rotation owner. Escalation path and
the finance owner to loop in are undefined at this stage._

## 4. Outline (sections to be filled in as the system lands)
1. Triage — confirm the mismatch is real and not a reporting artefact
2. Containment — stop automated release/sweep paths from acting on bad data
3. Diagnose — identify the unbalanced journal / missing entry
4. Correct — via stornation (offsetting `ADJUSTMENT` journal), never a direct balance edit
   (SPEC §15.4; journals immutable)
5. Verify — reconciliation returns zero-mismatch and stays clean
6. Report — incident record; notify the finance owner

## 5. Related
- Alert source: SPEC §22 Observability & Runbooks
- Ledger invariants: SPEC §11 (journals), §15.4 (stornation)
- Phase that populates this stub: Phase 2 — Money (SPEC §23)

---
*Stub. Sections 1 and 3 are intentionally empty until the owning team is confirmed.*