# Runbook: sms-backlog.md

> **Status:** STUB — outline only. No procedure recorded yet.
> Sections below are the required headings per SPEC §22 ("Every page alert links its runbook").
> **Do not treat any step here as real.** Steps are filled in as the messaging engines land
> (Phase 0–1, SPEC §23). This document creates no requirements.

## 1. Purpose
_Not yet written. Intended scope: operator response when outbound SMS queue depth grows beyond
capacity and messages are not being delivered in a timely fashion._

## 2. Alerts that trigger this runbook
SPEC §22 alert names (exact, from the spec):
- `sms_dispatch_seconds` — latency metric feeding diagnosis
- SMS queue depth > 1000 for 15 min — page

Severity: **page**.

## 3. Who gets paged
_Not yet decided — pending confirmation of the on-call rotation owner._

## 4. Outline (sections to be filled in as the system lands)
1. Triage — confirm depth is real and identify the stalled queue
2. Diagnose — provider throttling, consumer saturation, or priority-queue starvation
   (SPEC FR-2509 defines priority queues; the ordering policy is not settled here)
3. Containment — scale consumers; apply backpressure to producers
4. Verify — `sms_dispatch_seconds` returns to normal; depth drains
5. Recover — confirm no permanently lost messages; reconcile the affected window
6. Report — incident record; check whether any normative delivery window was breached

## 5. Related
- Alert source: SPEC §22 Observability & Runbooks
- SMS budget (UCS-2) constraint: SPEC FR-1005 — longer messages cost more segments,
  which affects throughput assumptions
- Phase that populates this stub: Phase 0 (messaging engine) / Phase 1 (SMS parity, SPEC §23)

---
*Stub. Sections 1 and 3 are intentionally empty until the owning team is confirmed.*