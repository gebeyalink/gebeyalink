# Data-breach procedure

**Summary:** Covers response to unauthorized access, disclosure, alteration, or destruction of personal data held by the platform.

## Detection
- **Who can detect:** Any staff member, security tooling alert, customer report, or partner notification.
- **Where alerts land:** On-call channel / email from CI/CD and infra alerting. At pilot, there is **no automated security-information event pipeline** in place; alerts are received by the on-call ADMIN via email from CI/CD and infra alerting.
- **First action:** Do **not** attempt to remediate before the case is opened. Preserve evidence. Treat the SPEC §22 audit log as the primary evidence source. The `audit_log` table is append-only (no one, including staff, can delete from it per its trigger).

## 24-hour internal assessment
Within 24 hours of detection, the **on-call ADMIN** and **SUPPORT** lead convene (roles per SPEC §5.2; **VERIFIER** may be consulted as needed). Classify:
- Is personal data involved?
- Is it at risk of further disclosure/alteration/destruction?
- What is the likely scale/scope?

Decide whether external notification is required. This decision is time-bound to 24 hours from detection.

## Regulator/partner notification, as legally required
Notification to regulators/partners depends on jurisdictions and partners in play (e.g. NBE for financial conduct, the Ethiopian Data Protection Proclamation once in force, partner bank/PSP contractual clauses).  
**Legal owner:** TBD — owner to fill (O-?). The legal owner (not engineering) makes the call. Engineering provides facts from evidence (audit_log, incident record) on request.

## User notification, when PII is at risk
If PII is at risk, notify affected users only, in their language, via the primary channel the account is reachable on (SMS for feature-phone users, in-app for app users — see notification channel taxonomy §11.1). A notification must state: what happened, what data was involved, when it occurred, what affected users should do, and how to reach support. Only affected users are notified.

## Communication and record-keeping
- Create a single incident record in an operator-chosen location (e.g. `infra/runbooks/incidents/` or the pilot ticketing system). One record per incident.
- Retain the incident record per **PRV-01**'s financial-record rule (7 years pending O-5).
- Retain consent/terms facts relevant to the incident per **PRV-02**'s permanent retention requirement.
- Preserve evidence (including audit_log references) in the record.

## Post-incident review
Required within **14 days** of closure. The review must be written, retained, and must include at least one finding that changes a control or a runbook. Link the review to the incident record.

## Cross-references
- **SEC-07** — audit log, append-only (primary evidence)
- **SEC-11** — webhook signatures (common breach vector)
- **PRV-01** — retention rules
- **PRV-02** — consent/terms retention
- **NFR-05** — durability (RPO 15 min, RTO 4 h); recovery clock is part of assessment
- **SPEC §5.2** — roles (ADMIN, SUPPORT, VERIFIER)
- **SPEC §11.1** — notification channels (SMS/in-app)
