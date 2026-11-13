# Student Finance Backend

YottaFlex challenge: calculate tuition and fees, apply scholarship eligibility and payments,
and reconcile student balances. Django 5.2 LTS + Django REST Framework + SQLite.

Design: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md), [`docs/DATABASE_DESIGN.md`](docs/DATABASE_DESIGN.md) (v1.1, frozen).

**Status: Phase 4, payments and refunds** (on top of Phase 1: students, programs, terms,
enrollments; Phase 2: fee schedules, invoices, waivers, statuses, void, late fees, the ledger
and the student ledger, statement and balance views; Phase 3: scholarship types, eligibility,
awards and their application). Phase 4 adds payments from any payer (each posting one PAYMENT
ledger entry), AUTO and MANUAL allocation to invoice lines as append-only rows, scholarship
precedence (payment money released last-in-first-out when a scholarship needs the room),
unallocated credit, and refunds (request → approve → process, or reject) capped by net paid,
the credit balance less pending refunds, and the source payment's available amount, posting a
REFUND ledger entry when processed. Withdrawals, cancellations and reconciliation are not
implemented yet.

Tested on Python 3.14.6 and Python 3.12 with the pinned requirements (Django 5.2.19).

**Unresolved design limitations:** learner "own record" and instructor "own programs" access
(ARCHITECTURE §13) are **blocked**, because the frozen model has no login↔student or
login↔program link. Learners currently have no access; instructors read programs and terms only.
Billing, scholarship, payment and refund data (Phases 2–4) is readable by `platform_admin` and
`support_staff` only; support staff may also request a refund.
See [`docs/KNOWN_LIMITATIONS.md`](docs/KNOWN_LIMITATIONS.md).

## Quick start

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt

.venv/bin/python manage.py check
.venv/bin/python manage.py migrate                          # domain database: db.sqlite3
.venv/bin/python manage.py migrate --database=ground_truth  # labels database: ground_truth.sqlite3
.venv/bin/python manage.py seed_terms                       # the 12 documented terms
.venv/bin/python manage.py seed_roles --demo-users         # role groups + demo API tokens

.venv/bin/python manage.py runserver
```

On **Windows** (PowerShell or cmd, Python 3.12 from python.org, `py` launcher):

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python manage.py check
.venv\Scripts\python manage.py migrate
.venv\Scripts\python manage.py migrate --database=ground_truth
.venv\Scripts\python manage.py seed_terms
.venv\Scripts\python manage.py seed_roles --demo-users
.venv\Scripts\python manage.py runserver
.venv\Scripts\python -m pytest tests/core tests/config tests/architecture
```

`DJANGO_SECRET_KEY` (env var) overrides the dev-only default key in `config/settings.py`.

| URL | What |
|---|---|
| http://127.0.0.1:8000/api/v1/health/ | health check (public) |
| http://127.0.0.1:8000/api/docs/ | Swagger UI |
| http://127.0.0.1:8000/api/redoc/ | ReDoc |
| http://127.0.0.1:8000/api/schema/ | OpenAPI 3 schema |

### Billing API (Phase 2)

| Method | Path | Notes |
|---|---|---|
| GET, POST | `/api/v1/fee-schedules/` | filters `program`, `term`, `fee_type`, `residency`, `in_force_on`; POST creates version 1 |
| POST | `/api/v1/fee-schedules/{id}/new-version/` | `{amount, effective_from}`; supersedes the latest version |
| GET | `/api/v1/fee-schedules/{id}/history/` | every version with derived `effective_to` |
| POST | `/api/v1/invoices/generate/` | `{enrollment_id, waivers?}` → 201; 409 duplicate / not ENROLLED; 422 no fee version |
| GET | `/api/v1/invoices/`, `/api/v1/invoices/{id}/` | amounts, both statuses; detail adds lines with open amounts |
| POST | `/api/v1/invoices/{id}/void/` | `{void_date}` (required) |
| POST | `/api/v1/billing/late-fees/assess/` | `{as_of_date, dry_run}` (`as_of_date` required) |
| POST | `/api/v1/billing/collection-status/refresh/` | `{as_of_date}` (required) |
| GET | `/api/v1/students/{id}/ledger/`, `statement/`, `balance/?as_of=` | student account views |

Writes need `platform_admin`; reads `platform_admin` or `support_staff`. There is no fee-schedule
detail route, and no PUT, PATCH or DELETE on financial records. Fee schedules come from the
API (or test fixtures); there is no seed command for them yet.

### Scholarships API (Phase 3)

| Method | Path | Notes |
|---|---|---|
| GET, POST | `/api/v1/scholarship-types/` | types are immutable; `program_ids` required when `program_restricted` |
| POST | `/api/v1/scholarships/eligibility-checks/` | `{student_id, term_id}` → one result per type; `evaluated_on` = the term's invoice date |
| GET | `/api/v1/eligibility/` | every evaluation, eligible or not |
| POST | `/api/v1/scholarships/` | `{eligibility_id, invoice_id, requested_amount?, award_date?}` → OFFERED |
| POST | `/api/v1/scholarships/{id}/apply/` | `{allocated_on?}`; all-or-nothing; OFFERED → ACCEPTED → APPLIED |
| GET | `/api/v1/scholarships/`, `/api/v1/scholarships/{id}/` | detail includes allocations and events |

Same permissions as billing. Four separate money limits: the per-term cap on **awards**
(BR-SCH-03), an award-stage reservation guardrail against the invoice's eligible fees, BR-SCH-04
on **applied allocations**, and each line's remaining room. The 7 documented types
(SCT-001…SCT-007) can be created with the opt-in command
`python manage.py seed_scholarship_types` (idempotent; refuses if other types exist; never run
automatically).

### Payments and refunds API (Phase 4)

| Method | Path | Notes |
|---|---|---|
| GET, POST | `/api/v1/payments/` | POST `{student_id, amount, received_date, method, payer_type, payer_name?, external_reference, auto_allocate?}`; 409 on a duplicate `external_reference` |
| GET | `/api/v1/payments/{id}/` | with `net_allocated`, `refunded`, `available`, `allocation_state` and the allocation rows |
| POST | `/api/v1/payments/{id}/allocate/` | `{mode: "AUTO", allocated_on?}` or `{mode: "MANUAL", allocated_on, lines: [{invoice_line_id, amount}]}`; AUTO reports `outcome` ALLOCATED / NO_OPEN_LINES / NO_FUNDS_AVAILABLE |
| GET | `/api/v1/payments/{id}/allocations/`, `/api/v1/payment-allocations/` | ALLOCATE and DEALLOCATE rows |
| GET, POST | `/api/v1/refunds/` | POST `{payment_id, amount, requested_date}` (reason OVERPAYMENT); 422 above a cap |
| GET | `/api/v1/refunds/{id}/` | with the request-time `calculation` and the status events |
| POST | `/api/v1/refunds/{id}/approve/`, `reject/` (`{on_date}`), `process/` (`{processed_date}`) | caps re-checked at approve and process |
| GET | `/api/v1/students/{id}/refundable/` | max refundable, per-payment available amounts, the calculation |

Writes need `platform_admin`, except `POST /refunds/`, which `support_staff` may also call.
In Django Admin the payments records are read-only (no add, change or delete).
Deallocation has no endpoint (it is a service used by scholarship precedence and, in Phase 5,
withdrawals). A payment holds at most 99 allocation rows (`409 ALLOCATION_LIMIT_REACHED`).

## Tests and quality

Everything implemented so far (Phases 0–4), including the runnable unit tests:

```bash
.venv/bin/python -m pytest tests/core tests/config tests/architecture tests/models tests/services \
    tests/api tests/commands tests/verification tests/workflow \
    tests/unit/test_invoice_status.py tests/unit/test_invoice_lifecycle.py \
    tests/unit/test_fee_schedule_versions.py tests/unit/test_billing_calculators.py \
    tests/unit/test_full_ride_scholarship.py tests/unit/test_scholarship_calculators.py \
    tests/unit/test_payment_allocation.py tests/unit/test_payment_calculators.py -rxX
#   2108 passed, 2 xfailed (the two blocked rules)
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

The other four files in `tests/unit/` are acceptance tests for later phases (ARCHITECTURE §14).
They cannot be imported until their modules exist, so a bare `pytest` (all of `tests/`) stops
at collection ("Interrupted: 4 errors during collection"). That is expected; use the command
above. To see their status:

```bash
.venv/bin/python -m pytest --collect-only -q \
    tests/unit/test_withdrawal_refund_pct.py tests/unit/test_withdrawal_release.py \
    tests/unit/test_worked_example.py tests/unit/test_stage_rng.py
```

`tests/verification/` is the API verification suite: every endpoint and method, roles,
ground-truth isolation, health outages, rollback, determinism, migration safety, concurrency,
and every response validated against the OpenAPI schema. Results:
[`docs/phase1_verification_results.txt`](docs/phase1_verification_results.txt),
[`docs/phase2_verification_results.txt`](docs/phase2_verification_results.txt),
[`docs/phase3_verification_results.txt`](docs/phase3_verification_results.txt),
[`docs/phase4_verification_results.txt`](docs/phase4_verification_results.txt).

## Layout

```
config/          settings (two SQLite DBs), GroundTruthRouter, URLs
core/            IDs, money (Decimal), domain errors + API error envelope, permissions,
                 SQLite system checks, IdSequence, StatusEventBase, health view
students/ academics/   Phase 1 apps
billing/         Phase 2: models, calculators (pure), lifecycle, policy, services, API;
                 allocations.py = registry through which higher apps' allocations are seen
scholarships/    Phase 3: models, calculators (pure), lifecycle, policy, services, API,
                 seed_scholarship_types command
payments/        Phase 4: models, calculators (pure), lifecycle, policy, services, API,
                 read-only admin
reconciliation/ datagen/
                 registered app skeletons; models arrive in Phases 6–7
tests/           core/, config/ (Phase 0); models/, services/, api/, commands/, verification/
                 (Phases 1–4); workflow/ (§9.3 steps 1–9); architecture/; unit/ (acceptance
                 tests; 8 runnable now, 4 for later phases); billing_fixtures.py,
                 scholarship_fixtures.py, payment_fixtures.py
docs/            design documents (frozen v1.1), KNOWN_LIMITATIONS.md, verification results
```
