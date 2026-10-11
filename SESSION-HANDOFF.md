# Session handoff — listings creation, lifecycle and price flagging

## What was built

A seller can post a crop, read it back, correct it, pause it, bring it back and close it. A
listing is live the moment it is posted, carries a short code a buyer can read aloud, and is
flagged rather than refused when its price sits far from what that crop last went for in that
region.

Nothing outside this slice moved. `SPEC.md` is unchanged, and the schema gained three tables
and nothing else.

## The decisions that are not obvious from the code

**A new listing is born in draft and published.** It goes through the same guard as every
later move, before the row is written, so "live on arrival" is a state the state table
produced rather than an initial value that bypassed it. Its first `version` is 1, not the
column default of 0: the version counts how many times a listing has been written, and a
listing nobody has edited has been written exactly once.

**Expiry is read, not written.** `effective_status` compares `expires_at` with the moment of
the request, so a lapsed listing reads as `EXPIRED` without anything having run and without a
GET changing a row. Two readers can never disagree, and the stored status does not depend on
who looked and when. The persistence worker the specification describes is not in this slice;
until it is, `EXPIRED` is a reading rather than a row, which is why the guard also reads the
derived state — otherwise a pause could land on a listing that had already left the market.

**A price is compared, never refused.** The band is `[0.2×, 5.0×]` and both edges are inside
it, because a price of exactly five times the average is unusual rather than wrong. Only a row
that declares itself usable is used at all: `insufficient_data`, or no average, means the
table declines to judge, and treating that as a price of zero would flag every seller in
exactly the regions we know least about. The comparison is integer arithmetic, so it is exact
at any price the schema allows.

**The offer's terms are not editable.** `crop_id`, `region_code` and `woreda_id` have no field
in the update schema at all, rather than a field that is quietly ignored, so a client that
tries is told. Swapping any of them would make the listing a different offer from the one a
buyer answered, and no amount of care afterwards makes that safe.

**A conflict hands back the row that won.** `CONCURRENT_MODIFICATION` carries the current
version and the current listing, so the caller can reconcile and retry rather than guess. A
no-op edit does not move the version, or two screens the seller has open would invalidate each
other for a submission that changed nothing.

**Photographs are URLs, not uploads.** There is no object storage in this slice, so a URL is
recorded and its shape is checked where it is written. What the caller said the picture is at
is what is kept.

## Two bugs the tests found, both now fixed

**Photographs could not be replaced.** `replace_photos` deleted the old rows and added the new
ones without flushing in between. A flush orders inserts before deletes, so the first new
photograph was written over the sort order of the old one still in the table, and the unique
constraint on `(listing_id, sort_order)` refused a perfectly good set of pictures. Any edit
that touched `photos` failed. The deletion is now flushed before the insertion.

**A lifecycle move could not return `updated_at`.** `updated_at` is written by the database
clock, so after an update the attribute has to come back from the same statement. Without
`eager_defaults` on the mapper, reading it issued a query from a plain attribute access, which
in an async request has no way to be awaited, and the response died with `MissingGreenlet`.

## What is deliberately not here

- USSD and SMS channels. An app posting is the only channel, and every listing is created
  `APP`.
- `Idempotency-Key` replay. Accepted at the edge, not yet honoured across a retry.
- The >10% price-change re-authentication, and the OPEN-offer quantity rules. Both arrive
  with FR-307; quantity is editable here, which is correct only because no offers exist yet.
- Agent posting on behalf of another seller, and the `on_behalf_of_user_id` that would carry
  it.
- The five-minute expiry worker.
- KYC and channel-role enforcement, the entity event log, and photo upload validation.

## State of the verification

| Gate | Result |
| --- | --- |
| `pytest apps/api/tests packages` | 348 passed |
| Scoped coverage, services and packages | 86.90% (gate 85%) |
| Overall coverage | 90.90% (gate 70%) |
| Unmarked layer marks | none |
| `mypy --strict apps/api packages` | no issues in 95 files |
| `ruff check`, `black --check` | clean |
| i18n key parity | 24 passed |
| Migration round trip | upgrade, downgrade, upgrade |

One observation, offered rather than fixed: coverage reports `listing_service.py` as 82% while
the HTTP tests demonstrably exercise `create` and `update`, and the line numbers it names as
missed are lines those tests run. Clearing every `__pycache__` and removing the `a1_coverage.pth`
tracer did not change the attribution, and the module resolves to the working tree rather than
an installed copy. Both thresholds pass with room, and the gates above are measured on the same
numbers CI will measure, so this is recorded as a reporting artefact rather than acted on.

Branch protection/rulesets require a paid plan on private repos (verified: personal account AND free org, Oct 2026) — enforcement advisory-only. Controls in force: orchestrated push-authorization loop (no push without green CI on exact commits), fail_action inside the security job, skipped-merge-gate signal on commits. Saved 'Protect Main' ruleset activates if the repo goes public.

## Traceability

`traceability/MATRIX.csv` rows FR-301 to FR-306 now read `DONE` for database, backend, API,
authorization and tests, with the deferred parts named in the note against each row rather than
left out of it. Two repairs were needed to write them: those six rows were wider than the
header, because the description opened with a quote that never closed before the category
column, and every comma in the requirement text had become a column separator. Each row is now
properly quoted, the file parses as 17 columns on all 154 rows, and no requirement text was
altered.

`evidence_reference` is `pending` on all six rows and takes the commit SHA once this is
committed.

---

# Task 8 — the merge gate, docs exposure, and a scan that is actually a scan

## The merge gate was a green tick that could not go red

`ci-green` decided with a shell loop over `${{ needs.*.result }}`. Outside `run:` that
wildcard expands to the single literal string `Array`, not one word per job, so the loop
compared `Array` against `failure`, never matched, and the job **passed** on run
`37202297387` while `zap-baseline` was failing in that same run.

The gate is now the job condition itself:

```yaml
if: ${{ always() && !contains(needs.*.result, 'failure') && !contains(needs.*.result, 'cancelled') }}
```

`contains()` is evaluated by the workflow engine, which is the context where the wildcard
does expand into every dependency result. `always()` stays, because without it a failed
dependency would *skip* this job, and a skipped merge gate does not block a merge the way a
failed one does.

**Gate correctness is proven by the first future red job, not by green runs.** A passing gate
on an all-green run is indistinguishable from the broken gate — both print a tick. The only
observation that tells them apart is a run in which some required job fails and this job is
*skipped* instead of passing.

**That observation now exists.** Run `37205038775` failed in `zap-baseline` for an unrelated
reason (see the action ref below) and `ci green (merge gate)` came back **skipped**, where the
old gate would have printed success on that same run. So the fix is confirmed on a red run, not
only tolerated on green ones. Note the consequence: the gate reports by *not running*. A
branch-protection rule that requires the check by name still needs to treat `skipped` as
blocking, which is a repository setting rather than a workflow one and was not changed here.

## The API no longer describes itself unless something asks

`Settings.expose_docs` (default `False`) decides whether `/docs`, `/redoc` and
`/openapi.json` are mounted at all. `apps/api/main.py` passes all three URLs to `FastAPI` as
`None` when the flag is off, which is how FastAPI is told not to serve a route.

`SPEC.md` §12.1 asks for the OpenAPI description to live in the repository as
`docs/openapi.yaml`, and it does. A *served* description is a different thing — a map of every
endpoint, including which take which secret, handed to anyone who can reach the port — and
nothing in the specification asks for it. **No file under `docs/` was changed.**

This needed one structural change that was not in the original plan. Deciding the route table
means reading the configuration while the application object is built, and the module had a
module-level `app = create_app()` — so importing `apps.api.main` would have demanded a
`DATABASE_URL` from anything that imported it, including tests that only want to look at a
route table. There is now no module-level `app`; uvicorn is pointed at
`apps.api.main:create_app --factory` (see `infra/Dockerfile.api` and the ZAP job). The
alternative, a second smaller settings object read only for this flag, would have declared the
same setting twice and put the environment-reading rule of `core/config.py` in two places.

## The scan is now pointed at the API rather than at a website

`zaproxy/action-baseline` → `zaproxy/action-api-scan`, `target` → `<bridge>/openapi.json`,
`format: openapi`. The baseline run it replaces found three URLs, all 404 (`/`,
`/robots.txt`, `/sitemap.xml`), because this API has no root document to crawl from, so its
66 passes were rules not firing against an error page. `action-api-scan` reads the description
and drives the scan from the endpoint list, which is why the boot step sets `EXPOSE_DOCS: true`:
without `/openapi.json` there is nothing to hand it.

`fail_action: false` and `allow_issue_writing: false` are both set explicitly even though the
action's defaults already are — so an upstream default change cannot turn a finding into a red
build before the findings have been read. `infra/zap-baseline-rules.conf` still holds no rule
overrides; its header now says the same file serves the API scan, because `rules_file_name` is
the input name both actions use.

**Not provable locally; the runner is the truth.** Whether the scanner inside its container can
reach the process on the bridge is runner truth: this machine has no `ip` to resolve a gateway
with, and Docker Desktop answers for a service bound to loopback where a Linux runner does not.
What *was* proven locally is that the process serves the description when asked and 404s it
when not — booted both ways against real PostgreSQL and real migrations:

```
EXPOSE_DOCS=false    /health 200   /docs 404   /redoc 404   /openapi.json 404
EXPOSE_DOCS=true     /health 200   /docs 200   /redoc 200   /openapi.json 200
                                openapi 3.1.0, 14 paths
```

## Two things the first run of each found

**`zaproxy/action-api-scan@master` does not exist.** The repository has exactly one branch and
it is `main`; `action-baseline`, which this replaced, is on `master`. The first push of the
API scan failed in 3 seconds at *Set up job* with `Unable to resolve action` — before any of
this code ran. Fixed in a fourth commit rather than by rewriting the pushed history, so the
mistake stays on the record.

**The first true baseline: run `37205542548`, all 14 jobs green.** The API scan read the
description and worked: `Total of 65 URLs`, `Number of Imported URLs: 19`, and

```
FAIL-NEW: 0  FAIL-INPROG: 0  WARN-NEW: 2  WARN-INPROG: 0  INFO: 0  IGNORE: 0  PASS: 116
WARN-NEW: X-Content-Type-Options Header Missing [10021] x 2
WARN-NEW: Cross-Origin-Resource-Policy Header Missing or Invalid [90004] x 2
```

Compare the 3 crawled 404s and 66 passes of the scan this replaced. Artifact `zap_scan`,
28303 bytes. Those two warnings are the triage queue for the rules file: they are real
missing-response-header findings on an API, not artefacts of scanning an error page. Read
before triaging — 10021 and 90004 are both header-presence rules, and whether the right answer
is to add the headers or to record the rule as accepted is a decision, not a mechanical one.
`fail_action` stays `false` until that has happened.

The job keeps the id and display name `zap-baseline` / "ZAP baseline (security)" although it
now runs an API scan, because renaming a job changes the required-check name and branch
protection would stop matching it.

---

# Task 9 — nosniff everywhere, one accepted risk, and a gate that can close

## X-Content-Type-Options is now on every response, including the ones nobody sees

Rule 10021 warned on two responses, and `10021 X-Content-Type-Options Header Missing` is a real
finding rather than an artefact of scanning an error page. The header belongs on every response,
so it now goes out from one place:

- `apps/api/core/response_headers.py` holds `SECURITY_HEADERS` and `apply_security_headers()`.
- The request middleware applies it, alongside the existing `X-Trace-ID`.
- `apps/api/core/error_handlers.py` applies it too. This is the part that is easy to miss: an
  unhandled exception does not pass through your middleware. Starlette's `ServerErrorMiddleware`
  builds that 500 response itself, so the first version of this change left the header off
  precisely the responses where a missing header does the most damage. The test for the forced
  500 failed until `error_response()` was included, and it is in the suite to keep it honest.
- `apps/api/tests/test_response_headers.py` covers 200, 404, forced 500 (`raise_server_exceptions
  =False`), trace-header responses and the docs routes.

`357 passed, 3 warnings`; `ruff`, `black --check` and `mypy --strict` clean.

## 90004 is an accepted risk, and saying so took two silent failures to make real

The rationale, recorded in `infra/zap-baseline-rules.conf`: v1 consumers are the Flutter app and
server-to-server gateways; bearer tokens, no cookie auth, no browser-embedding scenario. Revisit
if a web client ships. The rule stops another origin loading a response and reading it, which
needs a cookie or a user opening this API in a page on another origin; neither exists here.

Writing that down was the easy part. Making the scanner *read* it took two failures, and both
failures were silent, which is the part worth remembering:

1. `fail_action: false` and the properties line `90004=IGNORE` looked like an accepted risk that
   was doing something. Run `37264756936` proved it was not: `WARN-NEW: 1` with
   `[90004] x 2` still there. Not "the override did not apply" — the file was never opened.
   `action-api-scan` counts a rule with `processLineByLine`, which reads
   `line.split("\t")[1].trim() === "IGNORE"`. With no tab at all that indexing throws, the throw
   is swallowed by the caller's `catch`, `plugins` comes back empty, and the action then omits
   `-c` from the command entirely:

   ```
   docker run ... zap-api-scan.py -t http://172.17.0.1:8000/openapi.json -f openapi \
     -J report_json.json -w report_md.md -r report_html.html      <-- no -c
   ```

   A green scan, and the risk acceptance was decoration.

2. `rules_file_name` is TSV, not `.properties`. From the scanner's own parser,
   `zap_common.load_config`: `#` lines are skipped, every other line needs at least two tabs or
   the scan exits 3, and the level must be one of `FAIL, IGNORE, INFO, PASS, WARN`. Verified by
   running the real parser inside `ghcr.io/zaproxy/zaproxy:stable` against the repository file:
   `config_dict: {'90004': 'IGNORE'}`. The file is also pure ASCII on purpose, because the scanner
   reads a local file with a plain `open()` and therefore takes the image's locale encoding
   (`C.UTF-8` today), unlike its URL path, which decodes UTF-8 explicitly. No reason for a
   security-gate file to sit on that dependency.

Commit `031f647`, then run `37268838565`:

```
docker run ... zap-api-scan.py -t http://172.17.0.1:8000/openapi.json -f openapi \
  -J report_json.json -w report_md.md -r report_html.html -c infra/zap-baseline-rules.conf
IGNORE-NEW: Cross-Origin-Resource-Policy Header Missing or Invalid [90004] x 2
FAIL-NEW: 0  FAIL-INPROG: 0  WARN-NEW: 0  WARN-INPROG: 0  INFO: 0  IGNORE: 1  PASS: 117
```

`-c` is in the command, 90004 moved from `WARN-NEW` to `IGNORE-NEW`, and 10021 is gone.

## The gate is armed, and arming it was the last step rather than the first

`fail_action` does not read the report. It reacts to the scanner's exit code, which is arithmetic:
`1` if any rule FAILed, `2` if any rule WARNed, `3` if the scan broke or found nothing at all,
`0` if there is at least one pass and nothing failed or warned. Ignored rules are not counted, so
an accepted risk cannot redden the build — it was triaged on purpose — while a new warning or
failure will.

So the order was the work: make the scan mean something, then read it, then triage each finding
as either a fix or a written risk, and only then arm the gate. A gate armed before that is a gate
armed against a scan whose entire world is three 404s.

Commit `341ae4d`, run `37269785550`: all 14 jobs green, `ZAP baseline (security)` and
`ci green (merge gate)` both success, same `-c` honoured and the same
`FAIL-NEW: 0  WARN-NEW: 0  PASS: 117`. The gate is on and it is not firing on nothing.

The arc, for whoever reads this next: a scan that could not fail → a scan pointed at a real API
description → two real findings → one fixed in code, one written down as an accepted risk with
its condition for reversal → a gate that fails on anything new. The reporting-only phase was not
a preliminary sketch of the gate; it is what made the triage decisions possible instead of
mechanical.

The repository moved to `gebeyalink/gebeyalink` (private org) during this task; `origin` now
points there and the push of `02da7ff` was the first authentication test against it, which
succeeded.

# Task 10 - offers: placing one, answering it, and the depth a chain stops at

## A seller cannot offer on their own listing, and it is a validation error

The rules as written bound the price and the quantity but never say who may place a bid. Taken
literally, a seller could post an offer on their own crop, and the ten-open-offer cap would then
be spent by one trader pricing against themselves. `self_offer` is therefore refused as
`422 VALIDATION_ERROR` naming `buyer_id`, alongside the price, quantity and validity fields,
rather than as `403 FORBIDDEN`: the seller is a perfectly valid caller and the row they asked
for is one that cannot exist. The reason is worth stating because the alternative reading is
reasonable - a seller countering is normal, and a seller bidding is not the same act.

## Answering is decided by the depth; refusing is decided by the listing

`thread_depth` says who wrote the terms on the table: even is the buyer, odd is the seller, so
the party who may answer is always the other one, and the party who made the terms may never
answer them. Parity is derived from the depth rather than stored as a column, because a stored
`proposed_by` is a second account of the same fact and two accounts can disagree. Nothing names
whose turn it is anywhere in the request.

Refusing is a different question and gets a different guard. `reject` is the seller's at every
depth, checked against `listing.seller_id` rather than against the depth - see the bug below.
A seller who has countered is speaking on their own terms at an odd depth, and parity would hand
those terms back to the buyer to answer. Withdrawing is the buyer's alone, at any depth, by the
`buyer_id` named on the row: a withdrawal is the absence of an answer, and letting a seller
withdraw would end a negotiation the buyer was never told about.

## A counter is a row, not an edit

Answering writes two rows: a new offer one level deeper in the same `thread_id` with the same
`buyer_id`, and the offer it answers marked `SUPERSEDED`. The buyer does not change along a
thread - that column names who the negotiation is with, and a seller's counter is still a counter
on the buyer's negotiation. Which side is speaking is the depth's job. Nothing is edited in
place, so afterwards the thread still reads as what was offered, what was asked back, and in what
order. Chains stop at depth 3; the fourth answer is `422 LIMIT_EXCEEDED` and the offer it would
have answered stays `OPEN`, because refusing to answer is not the same as answering with a
refusal.

## The cap counts the clock, not the stored word

Ten effectively `OPEN` offers per listing. "Effectively" is doing the work: a bid whose
`valid_until` has passed stops occupying a slot even though its stored status is still
`OPEN`, so the eleventh offer lands on a listing with nine live bids rather than one with ten
lapsed ones. The window is stored as an instant, computed once from the hours the buyer asked
for, because turning a moment back into hours means dividing and rounding, and a request for
exactly the minimum would come back as 23. Counting is not reservation: an open offer reserves
nothing, so ten honest bids for the same hundred kilos are all placeable.

## One function writes a status

`transition_offer_status` is the only place an offer's status is assigned, and it is what
placing, countering, rejecting, withdrawing and expiry all go through. It takes `from_status` as
an argument instead of reading it off the row, so a caller cannot pass a state the row is not
in, and creation passes `None` - the one move with no state behind it, allowed only to land on
`OPEN`. A lapsed offer reads as `EXPIRED` through `effective_status`, so no GET writes and two
readers cannot disagree; that is also why the guard reads the derived state rather than the
stored one, otherwise a lapsed bid could still be rejected.

## The bug the tests found: the buyer could refuse the seller's counter

`reject` was gated on `may_answer`, which is the right question for a counter and the wrong one
for a refusal. At an odd depth - the seller's own counter - `may_answer` names the *buyer* as the
party entitled to answer, so a buyer could reject the seller's terms, and the seller could not
reject their own. The negotiation would end with the seller's "no" recorded under the buyer's
name, and a seller whose counter came back `REJECTED` would have no answer of their own to show
for it. `_require_rejectable` now asks the listing's seller, and both directions are tested over
HTTP; both tests fail against the old guard.

## What is deliberately not here

`POST /offers/{id}/accept` is absent rather than stubbed. Accepting an offer creates an `Order`
and reserves quantity in the same transaction (FR-1902, FR-505), and neither the order table nor
the reservation arithmetic exists yet. The `ACCEPTED` state is in the table and the guard permits
it out of `OPEN`, so the move is legal the moment the operation that performs it arrives;
nothing reaches it today. FR-503 is therefore recorded `IN_PROGRESS`, not `DONE`.

Also absent: the cron that persists `EXPIRED` for offers and listings, and any tier gate. The
`TIER_0+` label belongs to the mobile slice, and the app is not built.

## State of the verification

481 passed in the unit and integration layers. Every offer test drives the real app against an
ephemeral Postgres, and rows are read back out of the database rather than asked of the API, so
"the eleventh offer was refused and ten are still on the table" is checked against the table
rather than against the endpoint that refused it. Migration `0005` is exercised by the ordinary
migrated-database fixture, so the constraints it carries are created by the same run that relies
on them. Ruff, Black and strict mypy are clean.

One caveat, and it is a measurement problem rather than a product one: the local scoped coverage
gate reads 82.6% against a floor of 85%, and that number cannot be trusted on this machine. Code
that provably executed is reported as missed - a temporary `sys.stderr.write` inside
`OfferService.place` printed, and coverage marked that exact line uncovered - and untouched files
show the same thing: across 481 tests `auth_service` reports `if existing is None:` and its entire
body as never run. The same command on the pre-task tree reads 75.4% against a gate CI passes, so
the reading is depressed by more than ten points across the board rather than caused by this
slice. The 70% aggregate gate passes locally. The scoped 85% gate is the subject of the note below.

## The coverage meter was losing the greenlet switches, and that was the whole story

The scoped gate read 82.18% locally against a floor of 85%, and the first reading of that was
"the meter is corrupt here, CI must be right". That was wrong, and the way it was found is worth
keeping: CI's own log for `profile_service.py` is character-for-character identical to the local
one, so the local instrument was not lying differently from CI's - both were wrong the same way.
Across the 481-test run, `coverage json` showed that in `offer_service.py` all 50 missing lines
contain no `await` and all 20 executed lines inside a coroutine do, and the same held on files
this task never touched: `profile_service.py` reported 15 of its 16 missing lines as non-`await`
lines, including `if existing is None:` and its `return`, which
`test_an_empty_patch_before_any_write_creates_no_row` exists precisely to drive.

The cause is SQLAlchemy's async engine, which runs its synchronous database work inside a
greenlet. A greenlet switch is invisible to the tracer, so the tracer lost the enclosing coroutine
at the first database `await` and only re-acquired it at the next one - which is why every
pure-Python statement after an await looked unexecuted while the lines that themselves awaited
looked fine. Adding `concurrency = ["greenlet", "thread"]` under `[tool.coverage.run]` fixes it
(`5d74149`): the same 481 tests then read 98.15% scoped against a floor of 85%, and 97.49%
overall against a floor of 70%. Both meters are truthful now, and `offer_service.py` turns out to
be fully covered, which is what 124 tests against it had been saying all along.

Two things follow, and both are about method rather than about this slice. The gate had been
judging an instrument that could not see a third of the statements in an async service, so its
86.90% baseline was itself an artifact and any new async service of real size would have pushed
it under the floor for reasons that had nothing to do with the code. And the escalation path -
evidence of corruption, then let CI arbitrate - is still the right path for a gate that cannot be
trusted, but it is narrow: it is for an instrument that is wrong, not for one that is
misconfigured, and the difference between those two is the difference between a mystery and a
one-line fix. Both meters now tell the truth, so both are worth reading.

---

# Task 11 - the money module, and the rounding convention it commits to

`packages/shared/money.py`, 59 statements, and not one float in it. Percentages are exact
fractions (`2/100`, `15/100`) applied to integers and rounded once, at the end, by a single
function. The fee is two percent of the subtotal bounded below by five birr and above by five
hundred, the VAT is fifteen percent of that fee rather than of the subtotal, and the three
`fee_payer` settings move only the split.

## Half-up, and why not Python's `round`

`round_half_up` is the only rounding rule in the module, and it rounds a half **up**. That is a
convention rather than a law, and the reason it was chosen is that the alternatives make a fee
depend on the parity of the digit in front of the point: a trader cannot predict a charge, and a
platform that cannot predict its own revenue cannot reconcile.

The obvious implementation is wrong twice over, and both faults are silent. Python's `round` is
half-to-**even**, and `0.02` has no exact binary representation, so `round(12_525 * 0.02)` is
**250** where exact half-up on 250.5 is **251**. Verified in this session, not assumed:
12,525 santim is a real reachable subtotal, and 2% of it is exactly 250.5. The same mistake shows
up on the VAT path - a fee of 510 santim carries exactly 76.5 of VAT, which must be 77 - and
25,500 santim is exactly the subtotal that produces a 510 fee. Both cases are asserted.

## The two rulings, and a third that followed from them

**`fee_payer` is configuration, defaulting to the buyer.** `FeePayer` is a string enum in the
money module and `Settings.fee_payer` reads it with `default=FeePayer.BUYER`, so `SPLIT_50` can be
written in an environment variable as plainly as it is spelled in code. The default is the buyer
because a marketplace that quietly taxes the seller changes the price a seller sees and one that
quietly taxes the buyer changes the price a buyer sees; making that a deployment decision rather
than a constant means the choice is visible. The default is asserted twice - on the setting's
model field and on the function signature - because a default that lives in configuration but not
in the call is a default half the codebase can forget.

**The module lives in `packages/shared`, not in `apps/api`.** It is pure arithmetic: no session,
no HTTP, no clock. The USSD layer and the mobile client both have to agree with the API to the
santim, and one implementation under `packages/shared` is how they agree. Placing it under
`apps/api/services` would have made the API the only thing able to be right about money.

**The odd santim in a split goes to the buyer.** `SPLIT_50` rounds the buyer's half up and gives
the seller the remainder, so the two always sum back to `fee_vat` exactly. Rounding both halves
independently loses a santim on every odd fee and VAT - 25,500 santim gives 587, and 587/2 is
293.5 - and a santim belonging to neither party is a santim the ledger cannot book.

## What is deliberately not here

No order computes a total yet. Nothing is wired: `order_totals` is called by tests and by nothing
else, and order creation consumes it in the next task. The module does not read configuration
itself, does not touch the database, and raises `ValueError` rather than a domain error, because
a negative quantity or a zero divisor is a caller bug and not a business rule.

`FR-602` is recorded `DONE` in the matrix with `validation_status` still `NOT_STARTED`, which
means the money math and its tests are complete and nobody has watched a real order use them yet.

## State of the verification

611 passed in the unit and integration layers, 130 of them new and none of which touch a
database. The two worked examples are asserted field by field, the floor, the point where two
percent and the floor meet (25,000 santim is exactly 2% of 500), the ceiling on both sides, the
VAT half, and the split remainder are each pinned.

The reconciliation is asserted as an identity over 54 combinations - three payers by six subtotals
by three delivery charges - rather than as two expected numbers, because a branch that created or
destroyed a santim would satisfy any single worked example and fail this.

The suite was also checked against two deliberate breakages, since a money test that cannot fail
is a comment. Replacing `round_half_up` with Python's `round` fails 7 tests; rounding both halves
of a split independently fails 14. Restored, all 130 pass.

`money.py` reads 100% - 59 statements, 12 branches, nothing partial - which is why it does not
appear in the default coverage report at all: `skip_covered` hides fully covered files. Scoped
coverage 98.27% against a floor of 85%, overall 97.57% against 70%. Ruff, Black and strict mypy
are clean, `SPEC.md` is byte-identical, and the coverage meter fixed in Task 10 still reads
truthfully over the new pure module.

# Task 12 - orders: stock held by a row, and the race that says so out loud

## An order is born `CREATED` and enters `AWAITING_FUNDING` through the guard

Creation is not an initial value. The row is inserted `CREATED`, and `transition_order_status`
moves it to `AWAITING_FUNDING` in the same transaction, so a state the guard does not allow cannot
be reached by creating a row with it. An order that exists is already waiting for money; there is no
moment at which one is visible and unaccounted for.

## The reservation is the stock, and it is a row rather than a number

`quantity_reservations` holds one row per order in `HELD`, `CONVERTED` or `CONSUMED`, and
availability is `listing.quantity_kg` minus the sum of those three. Nothing adds or removes stock by
editing the listing. That is the whole reason FR-1904 can be enforced with one comparison: the
committed kilograms are a sum somebody else wrote, not a field on the row being edited.

`RELEASED` exists and is not summed, so cancellation and expiry hand kilograms back by changing one
reservation's state rather than by adjusting the listing.

## A race loser is told it lost the race

Two buyers can both pass a validation against the same stock. The one that loses must find out why,
because `422 ORDER_EXCEEDS_AVAILABLE` and `409 CONCURRENT_MODIFICATION` ask the caller to do
different things: the first is a request to change its quantity, the second is a request to reload
and try again.

So a sale snapshots the reserved kilograms *before* it takes the listing lock, and compares that sum
with the one it reads after locking and sweeping expired holds. An increase means somebody else
committed stock in between, and the request is refused as a concurrent modification. An unchanged
sum means the listing is simply smaller than the request asked for, which stays a validation error.
The two cases were the same 422 before this slice, which is a distinction the caller could not make.

The obvious earlier version of this was wrong in a way the tests caught: it treated any difference
between the pre-lock and post-lock sums as a race, so a cancellation that happened to free stock in
the gap was reported as a race. Comparing against the *pre-lock* number means the race is an
increase only, which is the direction a race actually moves in.

## A paused listing with nothing left still reads as paused

`effective_status` derives `SOLD_OUT` only from `ACTIVE`. §10.1 has no edge into `SOLD_OUT` from
anywhere else, so a listing emptied while paused stays paused: it is not for sale, which is what its
seller asked for, and the seller who resumes it and finds nothing left has been told the truth by
the arithmetic rather than by a status that moved while nobody was looking. The derivation is the
same idea as the expiry derivation in Task 6, and it has the same consequence: two readers cannot
disagree, and the stored column does not depend on who looked.

Editing a listing down to exactly what its orders hold is therefore allowed, and is tested from both
directions - a listing with stock reads `SOLD_OUT` with its stored status untouched, and the same
listing paused reads `PAUSED`.

## A buyer may accept the seller's counter, and neither party may accept its own terms

`may_answer` reads the row's proposer from its depth rather than storing a flag, so the rule holds
at every point in a chain: a buyer who asked for the seller's counter can accept it, and the seller's
counter and the buyer's own opening bid are both refused. A rule that only let a seller accept would
make a counter a dead end that both parties had to re-type to close.

## Expiry is derived; one reader writes it

The funding window is 48 h from `created_at` and is read, not scheduled. `GET /orders/{id}` is the
one reader that writes the lapse and releases the reservation; `GET /orders/mine` derives without
writing, so listing a buyer's orders neither depends on who looked nor releases stock as a side effect
of a list request. A cancellation attempted after the window is `409 STALE_VERSION` and leaves the row
alone, which means the release happens exactly once and on a path that re-reads under lock.

## An idempotent retry replays the first answer, and says so when it cannot

`Idempotency-Key` is a UUID scoped to the account and kept 24 h. A repeat with the same body replays
the stored response - including its status code - without creating a second order. A repeat with a
different body is refused rather than silently answered with the first response, because two different
purchases sharing a key is a caller bug worth surfacing. A key that is still in flight is `409`, and
failures are not cached, so a refused request can be corrected and retried with the same key.

## What is deliberately not here

- Escrow, delivery, disputes and ratings. The state table carries their edges (§10.4 in full, 15
  rows) and this slice writes two of them: `AWAITING_FUNDING` on creation and `CANCELLED` on a
  cancellation or a lapse. `FUNDED` arrives with the escrow hand-off, which is where payment is
  actually observed.
- The 24 h replay window is Redis expiry rather than a sweep. A key presented after it has elapsed
  is treated as new and buys again, which is the wrong direction for a caller who retries very late -
  a database row with an explicit age test would keep honouring it, and that arrives with the sweeper
  the specification describes for other expiry.
- Order search, filters and any seller-facing order list. `GET /orders/mine` is the buyer's list and
  is keyset-paginated; a seller sees their orders through the listing.
- `POST /orders/{id}/weight` and the escrow hand-off, which arrive with the settlement slice.

## State of the verification

795 passed, 26 skipped in the unit and integration layers, 3 warnings. 208 of those tests are new and
the largest group is the state-table file: every one of the 15 stated edges is asserted allowed by
name, and every unstated pair of the 11 statuses is asserted refused (121 pairs, of which the 15
stated ones and the 11 identities are skipped as outside its claim).

    scoped  (apps/api/services + packages, floor 85): 98.24% reached
    overall (apps/api + packages, floor 70):           97.26% reached
    ruff:    All checks passed!
    black:   131 files would be left unchanged.
    mypy:    Success: no issues found in 131 source files
    i18n:    24 passed
    docker:  infra/Dockerfile.api builds
    zap:     FAIL-NEW: 0  WARN-NEW: 0  INFO: 0  IGNORE: 1  PASS: 117

The concurrency tests are real races rather than mocked interleavings: two transactions are released
from a barrier between the unlocked availability read and the `FOR UPDATE`, and the assertions are
that exactly one order exists, that the other response is `409`, and that the kilograms are held once
and not twice. `test_AC_19_1_availability.py` does the same thing for two buyers taking the last
kilograms of a listing.

`SPEC.md` is byte-identical, and `docs/openapi.yaml` needed nothing: it is the full planned surface
and already carries all five order routes.

---

# Task 13 — search and discovery (`GET /v1/search/listings`)

## What was built

A buyer can search the market and page through it. `crop_id`, `region_code`, `woreda_id`,
`price_min`/`price_max`, `harvest_date_from`/`harvest_date_to`, `organic`, `delivery_option`,
`seller_tier_min` and `radius_km` all combine with AND; the default order is nearest first for a
trader who has a point, newest first otherwise, with `price_asc`/`price_desc`/`harvest_date_asc`
available; pages turn by cursor; and the five excluded states plus the one visible one are
exactly the spec's list. Five indexes sit on `listings.status` plus a GiST on `woredas.geog`.

## The decisions that are not obvious from the code

**Three rulings about the radius, settled against the plan and recorded here.**
1. The centre is the buyer's `profiles.gps`, measured to a woreda's boundary via
   `ST_Distance(ST_Centroid(woredas.geog), profiles.gps)`.
2. A woreda with no `geog` is **excluded from a radius filter**, and sorts last under
   nearest-first — which is what the plan means when it says `radius_km=50` returns only woreda
   2 and the unmapped woreda 4 is excluded. A consequence is that while a point exists the
   "sorts last / unmeasured-cursor" branches are unreachable: every measured-eligible woreda
   that has a geometry gets a distance, so the only page containing an unmapped woreda is one
   with no radius at all, and distance is then not sort material in the first place.
3. A buyer without GPS: an explicit `radius_km` is **422 `VALIDATION_ERROR`
   (`radius_km=requires_your_location`)** — answered an empty or full country instead would be
   an answer to a question not asked; no `radius_km` means distance is silently ignored and the
   default falls back to newest-first.

Full text of the rulings was pasted in planning; the above is what they came to in code.

**The default radius applies only to someone with a point.** FR-401 says 1–500 with a default
of 50, but "within 50 km" needs a somewhere to measure from. With GPS and no `radius_km` the
horizon is 50 km; without GPS there is no radius at all. Read that way the default never
contradicts the 422 rule.

**A cursor carries order and position, not filters.** Crafted as base64url of
`sort|created_at|id|price|harvest|distance`, it names the order it was issued under and the
boundary it ended at. The client restates filters on every page, which is the only both-ways
safe convention once filters are opaque — and it is why the distance walk restates
`radius_km=300` on page two. Refusals are per-field: a stated sort that contradicts the cursor
(`sort=does_not_match_the_cursor`), a token never issued (`cursor=not_a_valid_cursor`), and a
well-formed boundary that left out the part its order measures by (`cursor=missing_unit_price_santim`).

**The distance is measured, used for sorting and filtering, and never sent.** It lives only
in the cursor, so the client cannot round-trip a stale measurement and the fence between the
platform's use of a farmer's point and what the point is allowed to tell a stranger stays whole.

**`_on_the_market` is `(ACTIVE AND expires_at > now()) OR SOLD_OUT`.** Expiry is a clock
question for active rows — that is what `effective_status` already meant — and a sold-out row
keeps its place even after its expiry passes, because it is a lead to the seller, not an offer
to be fulfilled. The five exclusions are positive: the query names what is in, so a state the
spec adds later cannot leak in unnoticed.

**The same presentation as the detail view.** A result is `to_response` with `available_kg`, so
it is field-for-field what `GET /listings/{id}` returns, with `region_code` and `woreda_id` — and
never `kebele`, `gps`, `seller_id`, `on_behalf_of_user_id` or `distance_m`.

**Offset is not implemented on purpose.** `offset` is not in the contract, so FastAPI does not
parse it, and a caller sending it gets the identical first page instead of a shifted window — a
test pins that, because a test asserting only that it is documented would pass against an
implementation that quietly honours it.

**5 DB indexes + 1 GiST.** Rows are filtered on `status` first in every sort, so each index
leads with `status`; keyset pages need the sort key and the identity, which is why `id` ends
every BRIN. `ix_woredas_geog` makes `ST_Distance(ST_Centroid(geog), point)` a bounded search
once the boundaries dataset lands.

## What the tests found and fixed

**The contradiction test asked for a cursor a small market would not give.** With two listings
and the default limit the first page's `has_more` is false and `next_cursor` is null, so the
"contradicted sort" request inherited no cursor at all and answered 200 — the test believed it
was raising the refusal. A page of one-of-two under `limit=1` is the smallest market that must
issue a cursor, and is what the test now stands on.

## What is deliberately not here

- FR-405's price board. This slice reads `price_indices` only for the existing `price_outlier`
  flag; the public daily board is its own task.
- A *seller*-facing search. Search is a buyer's tool against the public market; `status`
  filters and search across one's own listings stay out of it.
- Multi-select filters. `crop_id` is single per the contract; a future OR-set is a schema and
  an operator change, not a flag.
- A distance in the response, a `distance` sort parameter, or any sort but the four the
  contract names — each is outside FR-401/FR-402 and deliberately refused or ignored.

## State of the verification

    scoped  (apps/api/services + packages, floor 85): 98.32% reached
    overall (apps/api + packages, floor 70):           97.26% reached
    ruff:    All checks passed!
    black:   140 files would be left unchanged.
    mypy:    Success: no issues found in 140 source files
    unit:    432 passed, 26 skipped
    integration: 406 passed
    zap:     FAIL-NEW: 0  WARN-NEW: 0  INFO: 0  IGNORE: 1  PASS: 117

AC-4.1 is walked exactly as written and more: fifty listings stamped to one instant so the
order has nothing to stand on but the identity, paginated at twenty — fifty rows back, once
each, no duplicates and no gaps — and a second walk under the measured order landing on each of
three woredas' listings exactly once.

`SPEC.md` is byte-identical. `docs/openapi.yaml` already carried the endpoint, so it needed
nothing either.

# Task 14a - cash-on-delivery: a pilot order flow behind a flag (FR-2101, FR-2102, FR-2103)

## What was built
- buy-now gains an optional `payment_mode` (`ESCROW|COD`, default `ESCROW`). COD is refused
  while `settings.cod_enabled` is off (422 `VALIDATION_ERROR` naming the feature) and above a
  1,000,000-santim subtotal (422 `LIMIT_EXCEEDED` with both numbers; 10,000.01 birr stands one
  santim out). A refusal writes no order and no reservation.
- A COD order is born `CREATED` (it never enters the funding queue) and holds its kilos as
  `HELD` exactly like escrow; the default body still produces `AWAITING_FUNDING`, so clients
  written before COD work unchanged.
- `POST /orders/{id}/confirm`: party-scoped confirmation, idempotent per party. The second
  distinct confirmation moves `CREATED -> CONFIRMED`, stamps `confirmed_at` and converts the
  reservation `HELD -> CONVERTED`. Replay by the same party is a 200 no-op. Stranger -> 403,
  escrow/cancelled -> 409 `ILLEGAL_STATE_TRANSITION`.
- `POST /orders/{id}/cancel`: either party may cancel a COD order while `CREATED` (seller's
  reason `OTHER`, buyer's `BUYER_CANCELLED`), releasing the kilos; after `CONFIRMED` neither
  side is released (409 either way). Escrow is untouched: the seller still gets 403
  `seller_cannot_cancel_after_acceptance`.
- Migration `0008` adds nullable `confirmed_at`, `buyer_confirmed_at`, `seller_confirmed_at`.
  `seller_delayed` is derived at read time from `confirmed_at` + 72h and never stored.
- Guard: `ALLOWED_TRANSITIONS` gains `CREATED -> CANCELLED`.

## Decisions not obvious from the code
- The confirm route is the one mutation with no `Idempotency-Key`: the party distribution *is*
  the key, and a second tap by the same person cannot double-confirm by construction.
- The seller's cancel reason is `OTHER`, never `BUYER_CANCELLED` - that value would be an
  ordinary lie told about a seller's own departure.
- The cap is on the subtotal (quantity x unit price); delivery and fees are out. The default
  listing ask (32,500 santim/kg) is over the cap at any quantity >= 1, so the COD happy paths use
  cheaper listings on purpose.
- SPEC 10.2's table lists `CREATED -> CANCELLED` nowhere (its pre-payment life is
  `AWAITING_FUNDING`). The task text is authoritative: the guard and
  `test_order_transition_guard.py`'s SPEC_TABLE gain the edge, and `SPEC.md` stays
  byte-identical with the delta recorded here.
- `seller_delayed` is derived by the same clock-vs-window rule as `effective_status`: nothing
  holds a flag that could disagree with the clock.
- COD has no escrow, payment or ledger rows - and there is *no such table* in the schema at all,
  so "no money moves" is structural, asserted against `information_schema`.
- The `cod_*` conftest stack mirrors the `seller_pays_*` pattern. The engine and Redis client
  are process-global singletons bound to the app whose lifespan started them, so a test may
  never drive two clients at once; the flag-off test uses the default client, COD tests the COD
  client, and strangers are signed in through the COD client they will be served by.

## What the tests found and fixed
- The first pass wrote confirm's both-stamps block over-indented; ruff SIM102 and a syntax check
  caught the shape and black reformatted.
- The cap-boundary tests initially created escrow orders because the body omitted
  `payment_mode="COD"` - the dominating default is exactly the behaviour FR-2101 intends, and
  the tests now send COD explicitly.
- The stranger confirm/cancel tests first minted the stranger's token on the default client while
  serving the request on the COD client (the cross-application hazard the fixtures document);
  they now sign the stranger in through the COD app.
- The first ZAP run's console capture was lost to an interrupted pipeline; the rerun
  (`FAIL-NEW: 0`, `PASS: 117`) is the evidence.

## What is deliberately not here
- Escrow gains nothing: funding, delivery, dispute and the money tables (`payments`,
  `escrow_accounts`, `ledger_accounts`, `ledger_entries`) remain future work, and COD's "no
  money moves" is true precisely because none of it exists yet.
- No seller_delayed UI or escalation beyond the derived flag. No COD-specific dispute or refund
  path yet.
- `docs/openapi.yaml` is untouched (no commit in this repo's history has ever edited it; the
  precedent is honored). It documents `/orders/{id}/cancel` with an FR-2103 mention but has no
  `/orders/{id}/confirm` route and its buy-now body lacks `payment_mode` - a documentation
  debt recorded here for whoever next touches it.

## State of the verification
    scoped  (apps/api/services + packages, floor 85): 98.19% reached
    overall (apps/api + packages, floor 70):           97.32% reached
    ruff:    All checks passed!
    black:   144 files would be left unchanged.
    mypy:    Success: no issues found in 144 source files
    unit:    432 passed, 27 skipped
    integration: 426 passed
zap:     FAIL-NEW: 0  WARN-NEW: 0  INFO: 0  IGNORE: 1  PASS: 117

`SPEC.md` is byte-identical.

---

# Task 14b - fulfilment: marking, the delivered kilograms, and the tolerance that arbitrates

## What was built

- `POST /v1/orders/{id}/ready` - the seller marks a pickup order ready (`CONFIRMED ->
  READY_FOR_PICKUP`, PICKUP delivery only). A `SELLER_DELIVERY` order is refused 409
  `ILLEGAL_STATE_TRANSITION` with its own `delivery_method` in the details.
- `POST /v1/orders/{id}/shipped` - the seller marks a seller-delivery order shipped
  (`CONFIRMED -> SHIPPED`, `SELLER_DELIVERY` only; `TRANSPORT_PARTNER` is not a seller
  channel yet).
- `POST /v1/orders/{id}/confirm-pickup` - the buyer confirms with the six-digit `pickup_code`.
  A wrong code is 422 `VALIDATION_ERROR` `fields.pickup_code = "does_not_match"` and the order
  is untouched; the right code moves `READY_FOR_PICKUP -> DELIVERED` in the same transaction
  that consumes the reservation and nulls the stored code, which is what makes the code
  single-use. Too early is 409 with the order's `status` and the requested `DELIVERED`.
- `POST /v1/orders/{id}/confirm-delivery` - the buyer's confirmation for a shipped order.
  Requires `SHIPPED` (too early is the same 409), passes the shrinkage check below, then
  settles to `DELIVERED` and releases the escrow reservation. This is FR-605's "release
  requires delivery confirmation", and nothing else settles a delivered order.
- `PUT /v1/orders/{id}/weight` - the delivered kilograms are recorded at the handover.
  Last-write-wins, `order.version += 1` each recording, only while the order is `SHIPPED` or
  `READY_FOR_PICKUP`; a weight at or below 0 or above the recorded bound is refused 422
  `outside_recordable_range`.
- All five routes require an `Idempotency-Key` and go through `_once`. `confirm` remains the
  one header-less mutation, because the party distribution is the key there. A replayed key
  replays the stored first answer byte-for-byte; a new key after settlement is 409.
- Seller reads are masked: `order_response`'s `viewer_id` nulls `pickup_code` for the seller
  everywhere - detail, list, confirm, cancel - so nobody who handles the order sees the code
  that releases it.

## The decisions that are not obvious from the code

**The windows are lazy and derived, and exactly one reader writes them.** A ready order reads
`DELIVERED` once `ready_at + 24 h` has passed, and a shipped order once
`shipped_at + hold_hours` has; `effective_status` computes both, so two readers cannot
disagree and nothing has to run. A stale `GET /orders/{id}` is the single writer: it expires
the order, settles the delivery, consumes the reservation and commits, all inside `_once` so a
retry cannot double-settle. `GET /orders/mine` derives without writing, so a list request never
releases stock as a side effect - the same split Task 12 chose for the funding window. The
five-minute executor the specification describes is still not here; the hold for a shipped
order comes from the crop margin key FR-2508 (`crops.escrow_hold_hours`, default 72 h), read by
`crop_hold_hours_for_order(s)`.

**The tolerance is integer arithmetic, like every other number in this repository.** The
shortfall is allowed when `10_000 * delivered >= ordered * (10_000 - basis)` where
`basis = round(pct * 100)`. At the default 2.0% the boundary is exact: 980 of 1000 passes and
979 is refused, and `confirm_delivery` returns 422 `below_shrinkage_tolerance` carrying
`ordered_kg`, `delivered_kg`, `tolerance_pct` and `min_acceptable_kg` - the numbers a later
dispute needs are captured at the refusal, not reconstructed after it.

**The recorded bound is an upper limit, not a suspicion.** `max_recordable_weight` is
`quantity_kg * 105 // 100`, capped at 1,000,000 kg; 1,000 kg records up to 1,050 and refuses
1,051. A surplus that fits is a legal delivery (1,050 of 1,000 settles fine); one that does not
is refused before the seller can type it.

**A method mismatch and a timing error are different refusals.** `ready` on a seller-delivery
order is 409 `ILLEGAL_STATE_TRANSITION` naming `delivery_method`; `confirm-pickup` too early is
409 with the order's stored `status` and the requested `DELIVERED`. Both are called out because
they ask the caller to do different things - change the operation, or understand the order is
not there yet.

**Settlement goes through the guard.** `_settle_delivery` moves `READY_FOR_PICKUP` or
`SHIPPED` to `DELIVERED` via `transition_order_status`, so the state table stays the one
authority on which edges exist (both are added to `ALLOWED_TRANSITIONS` and to
`test_order_transition_guard.py`'s `SPEC_TABLE`).

## What the tests found and fixed

- **The per-crop hold-hours join cross-multiplied.** The first repository version joined the
  orders without naming a `select_from`, so the constraint columns joined against each other and
  the query returned one row per cartesian combination. Added `.select_from(Order)` before the
  joins in both the single and the batch method; the integration suite's expiry tests were
  failing on exactly that.
- **Seller masking broke a "reads are identical" test on purpose.** `test_FR_608` asserted the
  buyer and seller could read the same order and get byte-identical bodies; masking is the whole
  point of `viewer_id`, so the test now pops `pickup_code` from both bodies, asserts the rest is
  equal, and asserts the seller's code is null while the buyer keeps theirs.
- **A unit test's own arithmetic was wrong before the code was.** The first draft of the
  shrinkage-boundary test expected 2.1% to block 979 of 1000; the rule as written does not -
  979 is exactly the 2.1% boundary and passes. The test now asserts the actual edges (979
  passes at 2.1%, 978 blocks; 980 passes / 979 blocks at the default 2.0%) and the
  implementation matches.
- Integration also caught a fixture detail: a seller-delivery listing must carry
  `delivery_fee_santim > 0` or the listing's own validation refuses it.

## What is deliberately not here

- The `WEIGHT` USSD command and a `COLLECTION_AGENT` role. FR-2507's text gives the collector
  the right to record the handover weight on a feature phone where the buyer is app-only; that
  route depends on ADR-E (role registry + channel matrix) and is recorded in the matrix rather
  than fabricated here. Until it lands, weight is recorded by the buyer over the API, and the
  USSD layer gains nothing in this slice.
- FR-606's payout split and any money movement on settle. "Release" here means the reservation
  is consumed and the order is DELIVERED; the ledger that pays the seller is a later slice.
- Any persistence sweeper for the windows, and any seller-facing order list beyond `read_mine`.
- `docs/openapi.yaml` is untouched, the standing precedent (`no commit in this repo's history
  has ever edited it`); `SPEC.md` is byte-identical.

## State of the verification

    scoped  (apps/api/services + packages, floor 85): 97.77% reached
    overall (apps/api + packages, floor 70):           97.09% reached
    ruff:    All checks passed!
    black:   147 files would be left unchanged.
    mypy:    Success: no issues found in 147 source files
    unit:    436 passed, 29 skipped
    integration: 446 passed
    unmarked layer marks: none (911 tests collected, none outside unit/integration/ussd/e2e/load)

20 integration tests cover the slice end to end: both fulfillment walks (pickup and
seller-delivery), the masking, wrong actor and wrong code refusals with the order untouched,
the two 409 timings, replay by the same key, the lazy expiries driven through the real 24 h/72 h
boundaries by backdated fixtures, the weight bound at 33/34/35, last-write-wins with the version
bump, and the tolerance boundary at exactly 980/979. `test_FR_701_702_703_fulfillment.py` and
`test_FR_2507_weight_and_tolerance.py` sit next to the other FR-* walk files; the covered
`order_service` gaps are pre-existing 404 branches.

**ZAP could not run locally, and the box does not pretend otherwise.** Both attempts through
`zap_local.ps1` failed the same way: `zap-api-scan.py` waits 600 s and never connects to its own
daemon. The raw image reproduces it - `ghcr.io/zaproxy/zaproxy:stable` (2.17.0) ignores `-port
8095` and binds two random ports, its `/health` is `unhealthy`, and the daemon hangs right after
`There is/are 14 newer addons`; ZAP's addon marketplace host `zap.storedtm.co.uk` is unreachable
from this machine (`curl` 000 in 1.3 s, GitHub reachable). Two of the three `-config`
knobs aimed at the update check are ignored, so this is a tooling/environment defect with a
clean upstream repro, not a scan run and not an application verdict. The authoritative gate is
CI's `zap-baseline` job (Task 8/9 wiring, `fail_action: true`), which boots the API and scans
`/openapi.json` from a runner with working egress; the same commit's ZAP result is read from
that run rather than invented locally. It was: run `37512698826` for commit `bfc068e`, all 14
jobs green, `FAIL-NEW: 0  FAIL-INPROG: 0  WARN-NEW: 0  WARN-INPROG: 0  INFO: 0  IGNORE: 1  PASS: 117`
— unchanged from the Task 14a baseline, so the five new routes introduced no new finding.

`traceability/MATRIX.csv` rows FR-605, FR-701, FR-702, FR-703, FR-2507 and FR-2508 now read
`DONE` for database, backend, API, integration, authorization and tests, with `overall_status`
`IN_PROGRESS` exactly as the neighbouring order rows carry it; `evidence_reference` names the two
new test files per the house convention.

**Dispute-awareness predicate ownership.** The snapshot that records `state_before_dispute` the
moment an order enters `DISPUTED` belongs to the disputes slice, which owns that transition; this
slice never writes the column. What exists here is the read side: the guard restores a rejected
dispute to whatever it holds (the sole extra exit out of `DISPUTED`,
`transition_order_status`, order_service.py:237-238) and every order response echoes it back
(`to_response`, order_service.py:1464). The write side lands with the disputes work.

**FR-2201 matrix-row staleness.** `traceability/MATRIX.csv` row FR-2201 (v1.0 commodity
restriction - only `base_unit = KG` crops) still reads `NOT_STARTED` on every layer although the
requirement is implemented: `crop_repository` serves only `CropBaseUnit.KG` rows and
`test_FR_2201_crops_endpoint.py` asserts no `UNIT` crop is listed. The row was corrected in
Task 15a: DONE for database, backend, API, integration, authorization and tests, `IN_PROGRESS`
overall, with evidence_reference naming the test file.

## Task 15a — SMS template registry, language budgets, and priority queues

One commit, one push: `d38f701`. CI: `ci` run `37737542278` and `deploy` run `37737542289` both green — 14/14 jobs, ZAP baseline PASS 117, total coverage 97.18%, scoped gate (>=85%) and overall gate (>=70%) both reached.

The message catalogue became data. Four YAML files under `packages/shared/i18n/templates/`
(auth, order_status, transactional, digest) hold ten templates — one OTP, four order
lifecycle states, three offer outcomes, two digests — each declaring its id, channel (sms
only for now), priority lane, placeholders, and am/om/en variants (ti optional, currently
unused everywhere and therefore skipped). `packages/shared/i18n/templates.py` loads and
validates them, keyed by id, and raises `ValueError` on any malformed entry (missing
field, unknown priority, unknown variant language, duplicate id, non-mapping entry),
naming the offending template where it has one.

`packages/shared/messaging/` (new package, registered in `[tool.setuptools]`) owns the
delivery policy. `queues.py` holds the `SmsPriority` enum and `priority_for()`, which
reads the declared lane out of the YAML — classification is data, not a hard-coded map —
plus `should_queue_until_quiet_hours()`, the [21:00, 06:00) EAT window decided by the
Ethiopia calendar day (EAT = UTC+3, no DST; represented as a fixed offset), with Q1
unconditionally exempt and no configuration knob. `provider.py` defines the
`MessagingProvider` protocol and the `SendResult` dataclass. No settings fields were
added in this slice; when sender-id or quiet-hours flags land they take `sms_*` names
with a suffix other than `_provider`, never touching `sms_provider`.

`apps/api/providers/sms.py` gained `AfricasTalkingProvider`, implementing the new
protocol and raising `NotImplementedError` until the O-2 merchant credentials resolve.
`RecordingSmsProvider` is untouched, and `build_sms_provider` is unchanged — the adapter
is not reachable from configuration yet.

Gates added: `test_NFR_07_sms_budget.py` (rendered-length budget, not source; Latin en/om
≤160 single / ≤153 concatenated, Ge'ez am/ti ≤70 / ≤67; a template under budget in source
but over after rendering fails; absent optional variants are skipped),
`test_FR_2509_priority_queues.py` (classification exhaustive and data-driven; an
unclassified template is rejected and named), `test_FR_1004_quiet_hours.py` (both edges
of both boundaries — 21:00 quiet, 06:00 not, 20:59 not, 05:59 quiet — Q1 exemption at
02:00 EAT, the EAT-calendar-day guard that catches raw-UTC-hour arithmetic, naive
timestamps refused), and `test_sms_provider_adapters.py` (adapter stub raises, recording
backend intact). Local gates are green; full suite results recorded in the evidence batch.

### Contract amendments pending docs-unfreeze
Four entries, no others yet:
1. `gps` on `ProfileUpdate`
2. `payment_mode` on buy-now
3. `POST /orders/{id}/confirm`
4. offer-acceptance COD selection

**FR-2201 matrix-row correction.** Applied in this slice (see the edited staleness note
above): DONE on database, backend, API, integration, authorization and tests, `IN_PROGRESS`
overall, evidence naming `test_FR_2201_crops_endpoint.py`.

**Fixture concurrency hazard.** Two concurrent pytest sessions clobber the shared
`gebeyalink-postgres` fixture container name; do not run two full-suite invocations in
parallel on this machine.

**Float boundary note.** `transit_shrinkage_tolerance_pct` is a float at the config
boundary only; downstream arithmetic is integer (`round(pct * 100)`); do not introduce
float tolerance math.

**`requested` asymmetry note.** `record_weight`'s 409 carries `requested:
delivered_weight_kg` (a field name); ready/shipped/confirm-* carry `requested: <target
state>`. Both are correct; do not unify.

**USSD idempotency-key ruling.** USSD mutations derive their idempotency key from
`(phone_e164, sessionId, operation, entity_ref)`; HTTP mutations use the
`Idempotency-Key` header. Same `_once`, two adapters. Recorded now for the USSD slice
that will use it.

**Template copy provenance.** Every message now declares whether its words come from the
specification verbatim (`spec_supplied`) or await a native speaker (`unreviewed`). The
only spec-supplied SMS text is the Amharic/English of `offer_received_buyer_sms` in
SPEC §14.5. No string in this registry is that text: the §14.5 copy runs on
`{listing_code}`/`{qty}`/`{valid_hours}`/`{token}` while this registry's `offer_received`
is authored over `{crop}`/`{price}`, so every string here is authored and marked
`unreviewed` — 0 `spec_supplied`, 10 `unreviewed`. O-3 native-speaker review is a release
blocker for Phase 1 exit: no unreviewed string ships to a real trader. A future slice
adding a string to any channel — SMS, USSD, push, in-app, admin — carries the same
obligation.

**Copy provenance is a standing rule.** Any slice that introduces human-language content
the SPEC does not supply carries an obligation to mark its provenance and route
unreviewed strings through O-3. This applies to SMS templates, USSD menu strings, push
copy, in-app notifications, admin console strings, help text, and any error message
outside the §27.3 catalog.

**Fixture-edit cost of required fields.** Adding a required field to a strict
loader (`review_status` in Task 15a) forces every test fixture that builds the
registry inline to gain the field. The Task 15a correction edited two existing
test files for exactly this reason; their assertions were untouched. This is
expected, not a defect. When a task package makes a field required, the builder
should expect fixture edits and disclose each one with proof it is a fixture
edit, not a loosened test. The prompt for such a task should say "assertions
stay the same" rather than "the file does not change."

---

## Task 15b — SMS inbound command grammar and dispatcher

One commit, one push: `c320abce0cb65d535af4d24d783f8c583c24c0bd`, subject
`feat(messaging): SMS inbound command grammar and dispatcher`. CI: `ci` run `37801301587`
and `deploy` run `37801301570` both green — 14/14 jobs, coverage 96.13% overall and 95.86%
scoped (both gates reached), ZAP baseline `FAIL-NEW: 0 FAIL-INPROG: 0 WARN-NEW: 0 PASS: 117`,
unchanged from the Task 14b/15a baseline.

**What shipped.** The inbound half of SPEC §14.4. `POST /v1/sms/inbound`
(`apps/api/routers/sms.py`, registered in `main.py`) proves `X-AT-Signature` over the raw
urlencoded body, reads `from`/`text`, parses, dispatches, renders exactly one template in
the trader's language, and answers the gateway with an empty 200 whatever the command did.
The reading half is `packages/shared/messaging/inbound.py`: a pure
`parse(text) -> ParsedCommand | ParsedUnknown` over the nine §14.4 verbs, no database, no
clock, no gateway. The deciding half is `packages/shared/messaging/dispatcher.py`, which
returns `(template_id, context)` and never a string — characters are produced only in
`routers/sms.py::_render`. Alongside them: `signature.py` and `action_tokens.py` in the
shared package; `packages/shared/i18n/templates/commands.yaml` (twelve reply templates) and
`_KNOWN_CHANNELS = frozenset({"sms", "ussd"})` in `templates.py`; `Settings.gateway_secret`;
`SmsCommandGateway` (`services/sms_command_gateway.py`) wired by `get_sms_gateway` in
`dependencies.py`; migration `0010_add_action_tokens_table` with `models/action_token.py`
and `repositories/action_token_repository.py`; the generic `once[T]` helper in
`core/idempotency.py` with an optional `key` parameter on the listing/offer/order services;
and four repository readers the SMS path needs that the HTTP path never asked for
(`crop_repository.find_by_name` and `find_by_id`, `listing_repository.find_by_short_code`,
`order_repository.find_by_short_code_for_party` — the last one scoped to a caller who is
already a party to the order, and documented as the single exception to that file's rule).

Twelve new test files: `test_FR_1102_sms_command_parser.py` (unit),
`test_FR_1102_sms_inbound_commands.py`, `test_FR_1102_sms_idempotency.py`,
`test_FR_1102_sms_gateway_signature.py`, `test_FR_1102_unknown_user_reply.py`,
`test_FR_1102_action_token_consume.py`, `test_FR_1102_action_token_purpose_binding.py`,
`test_FR_1102_sms_yes_accept_offer.py`, `test_FR_1102_sms_yes_confirm_delivery.py`,
`test_FR_1102_sms_yes_purpose_branch.py`, `test_FR_1706_payouts_command.py`,
`test_FR_405_price_command.py`.

**Rulings, one line each.**

- **Signing scheme.** `X-AT-Signature` is HMAC-SHA256 over the raw urlencoded bytes with
  `Settings.gateway_secret`, compared with `hmac.compare_digest`, verified *before* a byte
  is parsed (`packages/shared/messaging/signature.py`) — a field read out of an unsigned
  request is a field somebody else chose.
- **Corrected R5 record (supersession).** R5's "5xx for signature and malformed" is
  superseded: a bad or missing signature is 401 `GATEWAY_SIGNATURE_INVALID`, a malformed
  body under a valid signature is 422 `VALIDATION_ERROR`, both in the standard envelope,
  and no 5xx exists on this route — `errors.yaml` is untouched.
- **200 always.** A signed, well-formed request is answered 200 with an empty body whether
  the command succeeded or was refused for a business reason; a gateway that retried on a
  refusal would retry a POST that was already refused for a reason retrying cannot change.
  The reply the trader reads is the result; the gateway is told only "received".
- **Action-token gap closed.** `YES` and `NO` are the two sensitive commands; each consumes
  an action token before the domain sees it, so "is this permission real" is answered in
  the database rather than in the parser.
- **Migration 0010.** `0010_add_action_tokens_table` adds `action_tokens` (unique
  `token_hash`, `purpose`, `expires_at`, `consumed_at`); the `ActionToken` model and
  `ActionTokenRepository` ride with it. It is the only schema change in this slice.
- **Consume is one statement.** `consume_by_hash` is a single conditional update that
  spends the row only if it is unexpired and unspent, and commits on success, so the spend
  is durable before the domain action it authorised begins; it answers
  `ok | not_found | expired | already_used`.
- **Purpose encoding.** A token's purpose is `offer_answer:<offer_uuid>` or
  `delivery_confirm:<order_uuid>`, encoded when issued and decoded when spent; a purpose
  outside the two kinds never reaches a domain.
- **action_token_invalid (Q1).** Every failure inside the YES/NO token flow — wrong,
  expired, already used, unknown purpose, mismatched target, entitlement, and any business
  refusal after the consume — replies `action_token_invalid`, never `help`, never a domain
  error string.
- **Reply indistinguishability.** Because of that, a stolen valid token and a dead one
  produce the same sentence; no reply discloses which half of the check failed.
- **PAYOUTS (Q4).** `PAYOUTS` renders `payout_history_empty` with `count: "0"` and calls no
  payout service — there is no payout data behind this slice yet, and a count nobody can
  back would be worse than the empty one.
- **PRICE (Q4).** `PRICE` reads real rows: the quintal average is `avg // 100`, rendered as
  `Today Teff: 4,850 ETB/quintal`; a well-formed request with no usable row renders
  `price_unavailable` with `{crop}` and is not routed to help — the command succeeded.
- **Parser/dispatcher division.** `inbound.parse` is pure and returns a closed pair
  (`ParsedCommand` with only its verb's fields, or `ParsedUnknown` with a reason);
  `dispatch` takes that plus the trader and returns `(template_id, context)` and never a
  string.
- **Shared idempotency helper.** The SMS path reuses Task 15a's seam rather than growing a
  second one: `once[T]` in `apps/api/core/idempotency.py` now backs the listing, offer and
  order services too, which gained an optional `key` parameter they leave `None` for HTTP.
- **Key derivation.** The idempotency key is `sms:{from_phone}:{sha256(raw_text)}` over the
  raw bytes with no normalisation: the same words from two handsets are two intents, the
  same body twice from one handset is one.
- **Bit-identical retry.** A retried identical body writes one row and both attempts get
  the first answer back, byte for byte — asserted as `replies[-2] == replies[-1]` with the
  row count read straight from PostgreSQL.
- **Template list.** Eleven templates were ruled, then twelve by the ruling quoted below;
  every one is `unreviewed` on channel `sms` with am/om/en variants.
- **Unknown → help.** An unknown verb, a wrong argument count, an unparseable number and
  empty text are all `ParsedUnknown` and all render `help` — one sentence for "I did not
  understand you", and no `unknown_command` template, because there is no difference to
  express.
- **One template per reply.** Every reply is exactly one template id plus its context: no
  template is composed from another, and neither the dispatcher nor the router builds a
  literal string.
- **Loader channel set.** The registry's known channels are `{sms, ussd}`; a `ussd`
  template now loads and `push` still refuses by name. That is a loader change only — no
  USSD behaviour was written in this slice.
- **Field rules at the wire.** A `text` field that is present but empty is parseable and
  answers help; only an absent `from`/`text`, an empty `from`, or a body over 1600
  characters is a 422.

**Grammar coverage correction (the ninth ruling, verbatim).**

> Grammar coverage correction. Task 15b exercises all nine §14.4 verbs over the inbound
> endpoint with asserted replies; domain state is asserted for the five mutating verbs
> (POST, OFFER, YES, NO, CANCEL). The task package's "eight mutations" wording was a
> miscount and is superseded by this ruling.

**The three YES rulings (verbatim).**

> SMS YES on an offer-acceptance token builds OrderAcceptBody from the listing's offered
> delivery options (PICKUP preferred) and the buyer's profile (woreda_id,
> kebele/business_name/display_name); an incomplete profile replies
> profile_incomplete_for_order and creates nothing.

> YES decodes action_tokens.purpose; offer_answer:<uuid> → accept path;
> delivery_confirm:<uuid> → confirm path; any other or a state-invalid target →
> action_token_invalid.

> the SMS reply template list is now twelve; profile_incomplete_for_order is the twelfth,
> unreviewed, priority Q2.

**R11 (verbatim).**

> R11 grants a one-field waiver of the no-new-config-fields rule for gateway_secret,
> because the SMS inbound endpoint cannot verify its gateway without a secret source.

**The one workflow edit (verbatim).**

> gateway_secret is required (no default). The ZAP job's key-generation step also generates
> a throwaway GATEWAY_SECRET per run so Settings can boot. The scan does not exercise the
> SMS signature check, so the value is never matched against a real gateway. deploy.yml is
> offline-mode for the migration dry-run and does not need it.

**Process note.** When a note is said to live in another section, survey the adjacent
sections — headings first, then the phrase — before reporting it missing. That is how the
pointer below was reconciled with a file whose Task 15a section did not end where the
ruling assumed it did.

See the "Fixture-edit cost of required fields" paragraph in the Task 15a section.

**Deviations to disclose.**

- Migration `0010_add_action_tokens_table` — the task's token requirement could not be met
  without a table.
- Three repository readers that did not exist (`find_by_name`, `find_by_id`,
  `find_by_short_code`, plus `find_by_short_code_for_party`) — the SMS path looks up a crop
  by name and a listing/order by the code a human typed.
- The `once[T]` helper: `order_service`'s private claim/record block and the two new
  call sites were unified behind one generic function; behaviour is unchanged for HTTP.
- `gateway_secret` — a new required config field under the R11 waiver quoted above.
- Twelve templates where the earlier ruling said eleven (`profile_incomplete_for_order`).
- Test file renames: `test_FR_1102_action_token_check.py` became
  `test_FR_1102_action_token_consume.py` and `test_FR_1102_action_token_purpose_binding.py`.
- Three YES test files where one was specified (accept/confirm/purpose-branch), per the
  ruling quoted above.
- Fixture and test edits to two existing files: `test_NFR_07_sms_budget.py` gained
  placeholder fixture values for the new templates' placeholders (the budget is measured on
  rendered text, so each new placeholder needs a stand-in), and
  `test_FR_2509_priority_queues.py`'s lane test now names every id in three explicit sets
  with a union check instead of classifying Q2 by an `order_`/`offer_` prefix —
  `action_token_invalid` matches no prefix, and a prefix fall-through would have put an
  undecided reply in a lane by accident. Its property is unchanged: an id with no lane
  decision still fails the test, and now it fails loudly by name.
- Two existing tests construct `Settings` directly and gained the required
  `gateway_secret` line (`test_docs_exposure.py`, `test_response_headers.py`); their
  assertions are untouched.
- `conftest.py` gained `GATEWAY_SECRET` in the test environment plus the `sms_inbound`,
  `issue_action_token`, `stored_action_tokens` and `gateway_secret` fixtures.
- `.github/workflows/ci.yml` gained two lines in the ZAP job's existing key-generation step
  (throwaway `GATEWAY_SECRET`); it is the only workflow edit in this slice.

**Contract amendments: none.** `docs/openapi.yaml` has no diff and SPEC.md was not written
in this slice.

---

## Task 15c — USSD session engine, ingress, and the main menu trees

One commit, one push: `2eebf7a`, subject `feat(ussd): session engine, ingress, and the main menu trees`. CI: no CI verdict observed from here — this repository ships no `.github/workflows/` and no local CI runner resolves a run id; every gate that can run locally is green and recorded below, and the CI verdict is read from the repo's external run rather than invented.

**What shipped.** The USSD half of SPEC §14.1–§14.3, against the same registries, the same services and the same money arithmetic the app and the SMS channel use.

- `POST /v1/ussd/session` (`apps/api/routers/ussd.py`, mounted in `main.py`) proves `X-AT-Signature` over the raw urlencoded body, checks `sessionId`/`phoneNumber` present and the body utf-8 urlencoded, and answers with a `PlainTextResponse` — `CON` while the menu waits, `END` when the session is over. A bad or missing signature is 401 `GATEWAY_SIGNATURE_INVALID`; a malformed request under a valid signature is 422 `VALIDATION_ERROR`, both in the standard envelope. An empty `text` is the first press of a dial, not a malformed body.
- The session store (`packages/ussd/session.py`): `ussd:{sessionId}`, TTL 120 s sliding (every write resets it), holding `{user_id?, lang, menu_stack, context}`. A missing or corrupt session is a fresh start, and the fresh session's first screen drops the input that dialled it. A session loaded for a different phone number is a fresh start too (§R7).
- The trees are data (`packages/ussd/trees/`, 32 nodes over `main`, `listings`, `browse`, `orders`): each node names its registry entries, its `next(input, session)` choice map, the operations a choice triggers and the prefetches a screen needs before it renders. The engine (`packages/ussd/engine.py`) drives them; the gateway (`apps/api/services/ussd_gateway.py`) performs the operations over the same services.
- The five paths of §14.3 render: a listings menu (with Path 1's "New listing" as an `END` with an unreviewed placeholder — creating a listing from the handset is not in this slice), browsing by region then crop with result rows, an offer flow to a terminal "sent" screen, orders grouped by status, today's prices, and help. Language and draft resumes sit above the main menu.
- Mutations are idempotent and read side effects come from prefetches; no operation writes while a screen is being shown, and a retried press on a terminal screen replays the same screen without re-firing its operation.
- The language picked on the handset is stored under `ussd:lang:{phone}` for 30 days; a registered handset uses `users.language`; the first sign-in adopts the stored value unless the app itself states a language, in which case the app's newer word wins.

**Rulings, one line each.**

- **R5 record applies to USSD too (supersession).** A bad or missing signature is 401 `GATEWAY_SIGNATURE_INVALID` and a malformed body under a valid signature is 422 `VALIDATION_ERROR`, both in the standard envelope, and no 5xx exists on this route — the same correction Task 15b recorded for SMS, applying verbatim to the USSD ingress; `errors.yaml` is untouched.
- **Two channels, two derivations.** A USSD mutation's idempotency key is exactly `{session_id}:{operation}:{entity_ref}`, passed to the services; the services store it under their own `idem:{user_id}:` prefix exactly as the HTTP path stores its `Idempotency-Key`. The operations are `offer_place`, `offer_accept`, `offer_reject`, `delivery_confirm`. `OfferService.counter` takes no key: an offer carries only one row, whatever the earlier slice chose.
- **Gate.** The gate guards mutations only — reads are open, so an unregistered caller can browse, read prices and be told why there is no price, without being forced to register. A gated step hit by an unregistered handset ends with `register_prompt_ussd` (unreviewed, Q2, channel ussd) — the USSD sentence, distinct from the SMS channel's own `register_prompt`: two channels, two prompts, kept apart by id. The Path 1 gate sits in front of the whole listings menu; the browse path's gate sits at the "Make offer" step (a listing is worth showing a stranger and a bid is not).
- **Language.** The language menu adds `3. Afaan Oromoo`; the greeting is a separate `spec_supplied` entry from the menu. `ussd:lang` lives 30 days for an unregistered number only; a registered account's language is `users.language`, and OTP verify copies the stored `ussd:lang` value onto the account unless the app-side verification states a `stated_language` — theirs wins.
- **Provenance beyond the SMS rule.** `MessageTemplate.variant_review_status` is optional; when absent the loader fills `{lang: review_status}`, and an incomplete variant-level map is refused by name through `_parse_variant_review_status`. Registry-wide: `65` of `66` templates carry an unreviewed `om` variant; the only one whose `om` is not unreviewed is `ussd_price_row`, which declares `spec_supplied` and has no variant map, so its om is filled from the declared status.
- **Placeholders.** `placeholders` is required but may be `[]`; the loader's pinned message "placeholders must be a non-empty list" now applies only when it is non-empty. This conflicts with the earlier message wording — reported here, not papered over. The loader does not verify a body's `{name}` against the declared list; that remains a follow-up and is flagged in this section.
- **Lane.** Every `ussd.yaml` entry is `priority: Q2`; the lane test's Q2 set carries a three-line comment saying so, and every ussd id sits in `q2_ids`.
- **Refusals.** `ERROR_MARKER = "❌ "` in the renderer (docstring cites §14.1). A refused choice re-renders the *same* menu as the marker + reason + `\n` + the menu; the reason templates are unreviewed. A profile gap (missing woreda/kebele/delivery method) renders `error_profile_incomplete` with `{missing}` and does not count toward the tries.
- **The two helps.** The fourth consecutive invalid press ends the session on exactly `END Call 951 free / SMS HELP. Agents in your kebele can assist.` (the Path 5 USSD help template, `ussd_help`). The SMS channel's `HELP` command renders the commands.yaml `help` template. One help per channel, stated rather than merged, because they are different sentences owned by different slices.
- **No error-code work.** No `ErrorCode` was added or changed; `errors.yaml` is untouched. (R12.)
- **No new settings.** The USSD ingress reuses `gateway_secret`; no Settings field was added. (R11.)

**Deviations to disclose.**

- The row-selection actions (`select_region`, `select_crop`, `select_listing`, `select_offer`, `select_order`) fire as *source* actions on the screen that shows the list, not as arrival actions on the destination. The integration tests caught the arrival form: the destination's prefetch runs after the arrival, so the chosen row's list was not yet in context and every first navigation refused. The commit carries the source-action form, which the same tests assert end to end.
- `_prefetch_browse_results` coerces the chosen `crop_id` to `int` before building the search query ("smallint = character varying" in PostgreSQL otherwise); the context holds the string form from the digit press.
- `_op_offer_place` returns the listing's `{code}` so the terminal `ussd_offer_placed` screen can render "Offer sent: {code}".
- Fixture edits to three existing files, assertions untouched: `test_FR_1101_ussd_menu_navigation.py`, `test_FR_1101_ussd_tree_render.py`, `test_FR_2501_ussd_draft_resume.py` (unit tree/render/draft helpers) lost an unused import each; `test_NFR_07_sms_budget.py` and `test_FR_2509_priority_queues.py` were edited for the loader and lane changes above (budget stand-ins for new placeholders; the three-line Q2-set comment).

**State of the verification.**

    ruff:   All checks passed!
    black:  201 files would be left unchanged.
    mypy:   Success: no issues found in 200 source files
    full suite: 1066 passed, 29 skipped (12 warnings — StarletteDeprecation and SAWarning, both benign)

Eleven test files are new in this slice. Three are unit and run without a database (`test_FR_1101_ussd_tree_render.py`, `test_FR_1101_ussd_menu_navigation.py`, `test_FR_2501_ussd_draft_resume.py`); eight are integration (`test_FR_1101_ussd_ingress_signature.py`, `test_FR_1101_ussd_session_lifecycle.py`, `test_FR_1101_ussd_session_isolation.py`, `test_FR_1101_ussd_language_adoption.py`, `test_FR_1102_ussd_price_path.py`, `test_FR_1103_ussd_offer_path.py`, `test_FR_1104_ussd_order_path.py`, `test_FR_1105_ussd_help_and_unknown.py`). The six named evidence scenarios (menu navigation, draft resume, ingress signature, session lifecycle, session isolation, offer path) run verbatim: `34 passed`. The working tree is clean of `__pycache__`/`.pyc`/venv/coverage artifacts; `SPEC.md` is byte-identical and neither `docs/openapi.yaml` nor any infra, workflow, config or `errors.yaml` file changed.

**Follow-up flagged in this slice:** the loader does not verify a template body's `{name}` against the declared `placeholders` list — a future loader slice should refuse a body placeholding a name the list does not declare.

**Contract amendments: none.** `docs/openapi.yaml` has no diff, `SPEC.md` was not written in this slice, and no error code or Settings field changed.

## Architectural debt — revisit after the re-plan

Recorded 2026-10-09, before Slice A, as an acknowledging pass over the pre-slice baseline. These are recorded costs, not Slice-A work: no refactor during the re-plan; each is deferred to the milestone stated.

1. **order_service.py is 1,503 lines** (2026-10-09). It carries the state machine, the transition guard, the fulfillment operations, the weight/tolerance logic, the lazy evaluators for two order states, the response shaping, and the COD path. The split is deferred until the money spine and the pilot are stable. Line-count evidence: the pre-slice report.

2. **Flat layering vs. ARCHITECTURE §3's domain modules** (2026-10-09). The code uses `services/`, `routers/`, `repositories/`, `models/`; ARCHITECTURE describes `modules/{identity, orders, payments, …}`. The flat layering works today; the module split is target architecture, deferred. The payments module landing in Slice C is the natural point to check whether the split is becoming necessary.

3. **`GET /orders/{order_id}` is a read that writes** (2026-10-09). Reading an order lazily expires stale funding/pickup/shipment windows and commits (`order_service.py`, `_expire_if_stale`, commits at 1340/1347/1355 — documented "a read that writes"). It is intentional and covered by tests; revisit the lazy-commit semantics when the funding webhook path lands.

## Slice E scope decision — recorded before Slice A

Dated 2026-10-09. Decision recorded, not implemented:

- Slice E uses seed data for KYC-approved users and placeholder photo URLs for listing photos. It does not build `/me/kyc` or `/media/upload-url`.
- Slice E2 (new, before the pilot) builds `/me/kyc` and `/media/upload-url`, the latter with a real storage provider chosen by the owner.
- Reason: the scaffold proves the client pipeline, not the KYC or storage backends. Both backends have external dependencies (Fayda verification for KYC, in-country storage for media) that are not in place.


## Slice A1

- **Commit-body discipline note (2026-10-11).** The 5d0bbeb commit body was written by the builder in a bulleted form and named the SPEC erratum and the contract comparison, which are local records and not in the commit. The approved body was a one-paragraph subject+body. No rewrite; the deviation is recorded so future slices follow the approved body exactly.
