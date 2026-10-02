# Runbook: ussd-timeout-spike.md

> **Status:** STUB — outline only. No procedure recorded yet.
> Sections below are the required headings per SPEC §22 ("Every page alert links its runbook").
> **Do not treat any step here as real.** Steps are filled in as the USSD menu engine lands
> (Phase 0–1, SPEC §23). This document creates no requirements.

## 1. Purpose
_Not yet written. Intended scope: operator response when USSD response latency degrades
meaningfully above the 2 s p95 target. USSD sessions are carrier-timed and cannot queue,
so this degradation directly costs users their session._

## 2. Alerts that trigger this runbook
SPEC §22 alert names (exact, from the spec):
- `ussd_response_duration_seconds` — page when p95 > 2 s sustained 10 min
- `http_request_duration_seconds` — supporting signal for which upstream is slow

Severity: **page**.

## 3. Who gets paged
_Not yet decided — pending confirmation of the on-call rotation owner._

## 4. Outline (sections to be filled in as the system lands)
1. Triage — confirm the spike is real; identify whether USSD-only or shared with HTTP
2. Diagnose — DB contention, Redis (USSD state) latency, tree-rendering cost, or carrier-side delay
3. Containment — shed non-essential work; protect session-critical paths
4. Verify — p95 back under target; session timeout behaviour (SPEC FR-2501 draft/resume)
   still correct
5. Recover — confirm no orphaned sessions left in a broken state
6. Report — incident record; confirm SMS fallback path remained usable

## 5. Related
- Alert source: SPEC §22 Observability & Runbooks
- USSD/SMS parity requirement: SPEC FR-1103 (same operation behind both channels)
- NFR-01/NFR-02 load targets referenced by the §20 load layer
- Phase that populates this stub: Phase 0 (menu engine + trees) / Phase 1 (SPEC §23)

---
*Stub. Sections 1 and 3 are intentionally empty until the owning team is confirmed.*