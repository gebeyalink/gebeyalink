# Runbook: payment-outage-failover.md

> **Status:** STUB — outline only. No procedure recorded yet.
> Sections below are the required headings per SPEC §22 ("Every page alert links its runbook").
> **Do not treat any step here as real.** Steps are filled in as the payment integrations land
> (Phase 2 — Money, SPEC §23). This document creates no requirements.

## 1. Purpose
_Not yet written. Intended scope: operator response when a payment gateway degrades or fails —
primary gateway (Telebirr) with Chapa as fallback (SPEC §18)._

## 2. Alerts that trigger this runbook
SPEC §22 alert names (exact, from the spec):
- `payment_webhook_total{gateway,result}` — page when webhook failures > 5/h
- payment success rate < 90%/hour (derived from `payment_webhook_total`)

Severity: **page**.

## 3. Who gets paged
_Not yet decided — pending confirmation of the on-call rotation owner. Whether the payment/
partnerships contact is looped in for a primary-gateway outage is undefined at this stage._

## 4. Outline (sections to be filled in as the system lands)
1. Triage — identify which gateway and which direction is failing
2. Containment — enable the documented fallback path to Chapa
3. Verify — webhook signature verification and idempotency still hold (SEC-11);
   confirm the stuck-payment polling path (FR-2505) is picking up in-flight intents
4. Communicate — status to affected buyers/sellers; support line load expected
5. Recover — revert to primary once success rate recovers
6. Report — incident record; feed the partner if applicable

## 5. Related
- Alert source: SPEC §22 Observability & Runbooks
- Gateway behaviour and failover rules: SPEC §18, SPEC §15
- External dependencies: D-1 (bank), Telebirr, Chapa, D-7 (M-PESA merchant approval)
- Phase that populates this stub: Phase 2 — Money (SPEC §23)

---
*Stub. Sections 1 and 3 are intentionally empty until the owning team is confirmed.*