# Runbook: restore-drill.md

> **Status:** STUB — outline only. No procedure recorded yet.
> Sections below are the required headings per SPEC §22 ("Every page alert links its runbook").
> **Do not treat any step here as real.** Steps are filled in as backup/restore exists
> (Phase 0–1, SPEC §23). This document creates no requirements.

## 1. Purpose
_Not yet written. Intended scope: the monthly restore drill required by SPEC §20
("Chaos/backup | Monthly restore drill (NFR-05), ledger reconciliation drill | signed report").
This is a scheduled exercise, not a page-triggered incident response._

## 2. Alerts that trigger this runbook
**None — this runbook is not alert-triggered.** It is driven by a schedule (§20: monthly) and by
the annual cadence. Two §22 signals can *invalidate* a restore and must be checked as part of
the drill:
- `escrow_reconciliation_mismatch_total` — a restored database that fails reconciliation is
  not a successful restore; see `escrow-mismatch.md`
- `payment_webhook_total{gateway,result}` — replayed webhooks during the drill must be
  absorbed by idempotency (SEC-11 / FR-2505), not double-applied

Severity: n/a (scheduled); escalation follows the ordinary page path if the drill fails.

## 3. Who gets paged
_Not yet decided — pending confirmation of the on-call rotation owner, plus who signs the
report. §20 requires a signed report; the signing authority is undefined at this stage._

## 4. Outline (sections to be filled in as the system lands)
1. Pre-drill — record the target recovery point objective and confirm the backup exists
2. Restore to an isolated environment — never over live
3. Verify schema — migrations applied cleanly, PostGIS extension present (SPEC §18)
4. Verify ledger — reconciliation run; zero mismatch required
5. Verify idempotency — webhook replay does not double-apply
6. Measure — actual RPO/RTO vs NFR-05 targets
7. Report — signed drill report; record gaps as findings

## 5. Related
- Requirement: SPEC §20 Chaos/backup row; NFR-05 (restore drill)
- Escalates to: `escrow-mismatch.md` if reconciliation fails after restore
- Log retention context: SPEC §22 — 30-day hot, 13-month cold
- Phase that populates this stub: Phase 0 (backup tooling) / Phase 1 (SPEC §23)

---
*Stub. Sections 1 and 3 are intentionally empty until the owning team is confirmed.*