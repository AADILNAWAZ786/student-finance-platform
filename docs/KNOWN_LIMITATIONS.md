# Known limitations (unresolved) and Phase 2–3 policy decisions

Status as of the Phase 3 (scholarships) delivery (2026-11-13). L1 and L2 are **not implemented**.
The frozen design documents (`ARCHITECTURE.md`, `DATABASE_DESIGN.md` v1.1) are unchanged;
resolving L1/L2 needs a design decision by the project owner.

## L1: Learner "read own record" (ARCHITECTURE §13): BLOCKED

- **Design says:** a learner can read their own student record, statement and balance.
- **Why blocked:** the frozen data model has no link between a login (`auth.User`) and a
  `Student`, so the API cannot tell which student record belongs to a learner.
- **Current behaviour (tested, passing):** users in the `learner` group get
  `403 PERMISSION_DENIED` on every students, programs, terms and enrollments endpoint, and
  (Phases 2–3) on every billing and scholarship endpoint, including `/students/{id}/statement/`
  and `/balance/`.
- **Test:** `tests/verification/test_verify_security.py::test_design_learner_reads_own_record`
  is marked `xfail(strict=True)`. It reports as **XFAIL** until the rule is implemented. If
  it ever starts passing without a deliberate change, the strict marker turns it into a failure.

## L2: Instructor "enrollments for their programs" (ARCHITECTURE §13): BLOCKED

- **Design says:** an instructor can read enrollments for the programs they teach (no finance data).
- **Why blocked:** the frozen data model has no link between a login and a `Program`.
- **Current behaviour (tested, passing):** instructors can read programs and terms
  (master data only) and get `403` on students, enrollments and (Phase 2) all billing data.
- **Test:** `test_verify_security.py::test_design_instructor_reads_enrollments_of_own_program`,
  `xfail(strict=True)`.

**Resolving L1 and L2** needs a model addition, for example an optional one-to-one
`Student.user` and an instructor↔program bridge table. That changes the frozen design, so it
was not done in Phase 1.

## Phase 3 policy decisions (approved before implementation)

| # | Decision | Implemented as |
|---|---|---|
| D-S1 | How billing sees scholarship (and, later, payment) allocations | `billing/allocations.py` registry. Scholarships registers its provider in `AppConfig.ready()` (idempotent: same function = no-op; a different function under the same name raises). Billing imports neither scholarships nor payments (`tests/architecture/test_app_dependencies.py`). |
| D-S2 | Full-ride | At most one ALL_FEE_LINES type (partial unique index), and it must have category FULL_RIDE, group FULL_RIDE, priority 0 (DB CHECK + 400). Code recognises it by `coverage_scope`, never by the ID SCT-007. |
| D-S3 | Program lists | `program_ids` on `POST /scholarship-types/`: non-empty, no duplicates, every program existing when `program_restricted`; empty otherwise. Fixed at creation (types are immutable). |
| D-S4 | `requested_amount` | Must be > 0 (400) and may only reduce the computed award (422 above). |
| D-S5 | Per-term cap | `already_awarded` = cumulative `awarded_amount` of the student's non-revoked awards of the same type and term (awards, not allocations). |
| D-S6 | Eligible base vs line room | `eligible_fee_base` kept exactly as frozen (adjustments not counted). Apply separately keeps every line within net + adjustments − scholarship already on it. |
| D-S7 | Evaluation date | `evaluated_on` is always the term's `invoice_date`; clients cannot send it (400 "Unknown field."). The type's effective window is checked against that date. |
| D-S8 | Apply | Atomic, all-or-nothing; an award is applied once. |
| D-S9 | Other dates | `award_date` defaults to the later of evaluated_on and the invoice issue date; `allocated_on` to the later of the award date and the invoice's latest posting/status date. Never the clock. All must be 2026 business dates. |
| D-S10 | Seeding | `seed_scholarship_types`: opt-in, deterministic, idempotent; creates SCT-001…007 as documented, links SCT-004 to the STEM programs existing at that moment, refuses if other types exist. Never run automatically. |
| D-S11 | Used or superseded eligibility | 422 `SCHOLARSHIP_NOT_ELIGIBLE` with `details.reason` (no new error code). |
| D-S12 | Reversal | `scholarships.services.reverse_allocation` (no endpoint), for Phase 5's `reverse_for_adjustment`. |
| D-S13 | Acceptance | Apply records OFFERED → ACCEPTED → APPLIED (no accept endpoint). |
| D-S14 | Revocation | In the transition table only; no endpoint, no service (no REVOKE allocation reason exists). |
| D-S15 | Student status / enrollment | Not eligibility criteria (BR-SCH-01); an award still needs an ISSUED invoice. |
| D-S16 | Full reversal | Status stays PARTIALLY_REVERSED (no other status in the design); later reversals write no status event. |

**The four money limits are separate** (each one tested on its own in
`tests/services/test_services_scholarships.py`):

1. **Per-term cap on awards** (BR-SCH-03, D-S5): `award_amount(type, base, already_awarded)`.
2. **Award-stage reservation guardrail (not BR-SCH-04):** at award time the invoice's
   reservations plus the new award must fit its eligible fee base. An award reserves its
   `awarded_amount` while OFFERED/ACCEPTED and its net applied amount once APPLIED or
   PARTIALLY_REVERSED. It caps the award "at eligible fees" (§9.3 step 5) so that awards that
   could never be applied are not created. Its 422 carries `rule_id: null`.
3. **BR-SCH-04 on actual allocations**, at apply: Σ net APPLY/REVERSE allocations on the invoice
   after the apply ≤ `eligible_fee_base` (422, `rule_id: BR-SCH-04`).
4. **Each line's remaining room** (BR-ALC-02, D-S6), at apply, including adjustments
   (422, `rule_id: BR-ALC-02`).

**BR-ALC-06 (Phase 4 dependency):** apply asks billing's payment-release hook to free payment
money on the lines it covers. No payment allocations exist in Phase 3; a non-zero release with
no registered handler raises `PaymentReleaseUnavailable` and rolls the whole application back.
Phase 4 registers the LIFO deallocator.

## Phase 3 observations

| # | Observation | Current behaviour |
|---|---|---|
| P3-O1 | **Design inconsistency:** DATABASE_DESIGN gives `ScholarshipType.category` as Char(12), but lists the value `INTERNATIONAL` (13 characters). | Char(13), the minimum that stores every documented value. The design doc is unchanged; the schema contract test allows exactly this one documented exception. **Needs owner confirmation.** |
| P3-O2 | Seed categories: the design's seed table lists criteria, not categories. | SCT-001/002 MERIT, SCT-003 NEED, SCT-004 PROGRAM, SCT-005 INTERNATIONAL, SCT-006 BURSARY, SCT-007 FULL_RIDE. |
| P3-O3 | Reason codes: the design names two (GPA_BELOW_MIN, ALL_CRITERIA_MET). | Fixed vocabulary in `scholarships/policy.py`: GPA_BELOW_MIN, GPA_AT_OR_ABOVE_MAX, NEED_BELOW_MIN, RESIDENCY_MISMATCH, NOT_FIRST_TERM, PROGRAM_NOT_ELIGIBLE, TYPE_NOT_IN_EFFECT; every failure reported, in that order. |
| P3-O4 | Re-evaluations in one term share `evaluated_on` (D-S7). | "Latest" evaluation = highest `eligibility_id`. |
| P3-O5 | An unapplied award does not block a void (BR-INV-08 counts allocations, not awards). | The award stays OFFERED against a VOID invoice and cannot be applied (409). |
| P3-O6 | `/students/{id}/ledger/` entries gained a `scholarship_allocation` field (null for line entries). | Additive response change; one Phase 2 test's expected dict gained that key. |
| **D-S17** | **Phase 5 dependency conflict:** ARCHITECTURE §4 says `academics.withdraw_enrollment` orchestrates scholarships and payments, but academics sits below them in the same dependency graph. | Not resolved in Phase 3 (graph unchanged). Phase 5 needs a decision, e.g. orchestration in a higher layer. |

## Defects fixed in the Phase 3 delta after review (before merge)

| # | Defect | Fixed in |
|---|---|---|
| P3-B1 | `ScholarshipType.effective_from` / `effective_to` were not limited to 2026 business dates. | `create_scholarship_type` validates both with `check_business_date` (field-level 400, both reported together). |
| P3-B2 | The API accepted `max_gpa = 0` (approved 0–4 bounds) but the DB CHECK required `max_gpa > 0`, so a valid request hit the database. | Model and `scholarships/0001_initial` (not yet merged) aligned to `0 ≤ max_gpa ≤ 4`. |
| P3-B3 | Found while proving P3-B2: the IntegrityError handler in `create_scholarship_type` relabelled **any** database error as "Only one ALL_FEE_LINES type may exist". | It now maps only the full-ride unique-index race to that 400 and re-raises everything else. |

## Phase 2 policy decisions (approved before implementation)

| # | Decision | Implemented as |
|---|---|---|
| D1 | LedgerEntry is staged | Phase 2 has the `invoice_line` source and CHARGE, LATE_FEE, ADJUSTMENT. Phases 3–4 add `scholarship_allocation`, `payment`, `refund` and their entry types by additive migrations. `tests/models/test_billing_schema_contract.py` reads DATABASE_DESIGN.md and checks the difference is exactly these staged items. |
| **D2** | **Required fee types (explicit policy decision; the design does not list them)** | TUITION and REGISTRATION always; LAB only when `program.has_lab`; INTERNATIONAL_SERVICE only for international students. Missing → `422 FEE_SCHEDULE_NOT_FOUND` listing `missing_fee_types`. TECHNOLOGY, LIBRARY and STUDENT_ACTIVITY are charged only when a version is in force. (`billing/policy.py`) |
| D3 | Residency mapping | Exactly DATABASE_DESIGN §3: TUITION DOMESTIC or INTERNATIONAL; INTERNATIONAL_SERVICE INTERNATIONAL; every other type ALL. 400 from the API, and a DB CHECK. |
| D4 | Waivers | `price_lines(enrollment, fee_rows)` keeps its frozen signature; `apply_waivers(lines, waivers)` applies BR-WAV-01 separately. |
| D5 | Fee flags and allocation priority | Set by the server from `fee_type`; a request that sends them gets 400. Also DB CHECKs. |
| D6 | Billing access | write `platform_admin`; read `platform_admin`, `support_staff`; `academic_admin` and `instructor` 403; learner blocked (L1). |
| D7 | Student ledger / statement / balance | Documented paths, implemented in `billing` (dependency order). Scholarship, payment and refund parts are 0 / empty until Phases 3–4. `/refundable/` is Phase 4. |
| D8 | Batch dates | `as_of_date` required for late-fee assessment and collection refresh; no clock default. |
| D9 | Settings | Only `SPECTACULAR_SETTINGS["ENUM_NAME_OVERRIDES"]` gained 4 enum names. |
| D10 | Fee-schedule seeding | Deferred. No seed command; fee schedules come from the API or test fixtures. A future one must be opt-in, deterministic and idempotent. |
| D11 | Invoice for a non-ENROLLED enrollment | `409 ILLEGAL_TRANSITION`. |
| D12 | `release_for_adjustment` | Not implemented (Phase 5). `tests/unit/test_withdrawal_release.py` still fails to import. |
| D13 | Ledger balance | `billing.calculators.ledger_balance(entries, as_of)` is the single BR-BAL-01 implementation, used by `/balance/`. Phase 6 adds `reconciliation.calculators.balance_from_ledger(entries, as_of=None)` as a thin wrapper **with the parity tests** (they cannot exist before the wrapper does). |
| D14 | No `GET /fee-schedules/{id}/` | That URL returns the 404 envelope; `/history/` returns full records with derived windows. |
| D15 | Void date | `{"void_date": "YYYY-MM-DD"}` required, a 2026 business date, on or after the issue date (see O9). |

## Phase 2 observations (for review; behaviour chosen where the design is silent or ambiguous)

| # | Observation | Current behaviour |
|---|---|---|
| O8 | **Pricing date.** BR-FEE-01 (ARCHITECTURE) prices on the term's `invoice_date`; DATABASE_DESIGN's InvoiceLine rule says "in force on issue date". They differ only for a late enrollee when a version starts between the invoice date and `enrolled_on`. | Implemented per BR-FEE-01 (`term.invoice_date`, the rule with an ID and error code). Tested by `test_BR_FEE_01_version_starting_after_invoice_date_is_not_used`. Needs owner confirmation. |
| O9 | Void date versus later postings | Besides D15, `void_date` must not precede the invoice's latest posting or status date (e.g. a late fee), because event dates must not go backwards (DATABASE_DESIGN §3, StatusEventBase). 400 with the date. |
| O10 | Void amount per line | Minus the line's *remaining* net (net + earlier adjustments). Identical to BR-INV-08's "−net" when no adjustment exists (always in Phase 2); avoids over-adjusting after a Phase 5 withdrawal credit. |
| O11 | 2026 business-date range | Applied to `void_date`, fee `effective_from`, batch `as_of_date` and `/balance/?as_of=` (ARCHITECTURE D1: every business date falls in 2026). |
| O12 | Statuses and errors not reachable through the Phase 2 API (Phase 3: SCHOLARSHIP_COVERED and `INVOICE_HAS_ALLOCATIONS` are now reachable with real scholarships) | PAID, PARTIALLY_PAID, SCHOLARSHIP_COVERED and `409 INVOICE_HAS_ALLOCATIONS` need allocations (Phases 3–4). The calculator implements all six statuses (unit-tested); void reads allocations through one seam (`billing.services.allocations_for`), which tests and the Swagger replay use to prove the 409. |
| O13 | Late fee timing | Posted the day after `due_date + grace_days`, on what was outstanding at the end of the grace day, whatever date the batch runs. `dry_run` defaults to false. |
| O14 | Collection refresh scope | Skips VOID invoices and invoices already derived for a later date (never moves a status back in time); reports the counts. |
| O15 | Workflow IDs | ARCHITECTURE §9.3 shows IDs for the generated dataset (STU-010001, ENR-2026-030001); on an empty database the same calls give STU-000001, ENR-2026-000001 (`tests/workflow`). |

## Other observations from Phase 1 (no design rule exists; left as implemented)

| # | Observation | Current behaviour |
|---|---|---|
| O1 | Enrollment by students whose status is ON_HOLD, WITHDRAWN or GRADUATED | allowed (the design has no rule; not covered by a test) |
| O2 | Earliest allowed `enrolled_on` | no lower bound (only "≤ term start + 7"); early registration is tested as accepted |
| O3 | A term's `year` versus its dates | not cross-checked |
| O4 | Who may create students | `academic_admin` and `platform_admin` (§13 does not assign this explicitly) |
| O5 | POST to a URL without the trailing slash | 301 redirect (Django default); clients should use the documented slash-terminated paths |
| O6 | Unknown query parameters / ordering fields | ignored (DRF default) |
| O7 | Withdraw / cancel enrollment | arrives in Phase 5 with billing adjustments; WITHDRAWN and CANCELLED are terminal and tested as such |

## Defects found and fixed during Phase 1 verification

| # | Defect | Fixed in |
|---|---|---|
| B1 | Malformed IDs and unknown `/api/` URLs returned an HTML 404 page instead of the JSON error envelope | `core/api.py`, `config/urls.py`, `students/views.py`, `academics/views.py` |
| B2 | A decimal input of `"-0.00"` was stored and returned as `"-0.00"` | `core/fields.py` |
| B3 | List endpoints' 404 ("Invalid page.") was missing from the OpenAPI document | `core/schema.py`, both views |
| B4 | The B1 catch-all disabled the APPEND_SLASH redirect (regression, caught by a test) | `core/api.py` |
| B5 | OpenAPI error examples were copied from shared Student defaults: `gpa` 400s on programs/terms/enrollments/list/patch/status, "No Student matches" 404s on program/term/enrollment endpoints, and an impossible 422 (`credits: 30`, which is actually a 400). 14 examples in total. Documentation only; API behaviour unchanged | `core/schema.py`, `students/views.py`, `academics/views.py`, `config/health.py`; guarded by `tests/verification/test_verify_openapi_examples.py` |

## Defect found and fixed during Phase 2 implementation (before delivery)

| # | Defect | Fixed in |
|---|---|---|
| P2-B1 | The CHECK "adjustment_reason required on ADJUSTMENT lines" accepted NULL, because SQL `NULL IN (...)` is unknown, which a CHECK accepts. Found by `tests/models/test_models_billing.py` | `billing/models.py` (explicit `IS NOT NULL`), migration regenerated |

Per-case results: [`phase1_verification_results.txt`](phase1_verification_results.txt),
[`phase2_verification_results.txt`](phase2_verification_results.txt),
[`phase3_verification_results.txt`](phase3_verification_results.txt).
