# Student Finance Backend

YottaFlex challenge: calculate tuition and fees, apply scholarship eligibility and payments,
and reconcile student balances. Django 5.2 LTS + Django REST Framework + SQLite.

Design: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md), [`docs/DATABASE_DESIGN.md`](docs/DATABASE_DESIGN.md) (v1.1, frozen).

**Status: Phase 8, full-scale tooling** (on top of Phase 1: students, programs, terms,
enrollments; Phase 2: fee schedules, invoices, waivers, statuses, void, late fees, the ledger
and the student ledger, statement and balance views; Phase 3: scholarship types, eligibility,
awards and their application). Phase 4 adds payments from any payer (each posting one PAYMENT
ledger entry), AUTO and MANUAL allocation to invoice lines as append-only rows, scholarship
precedence (payment money released last-in-first-out when a scholarship needs the room),
unallocated credit, and refunds (request → approve → process, or reject) capped by net paid,
the credit balance less pending refunds, and the source payment's available amount, posting a
REFUND ledger entry when processed. Phase 5 adds withdrawals (BR-WDR-02 schedule: 80 / 60 /
40 / 0 % credit on refundable lines by day since term start) and cancellations before the
start (BR-WDR-01: every line but REGISTRATION credited, invoice CANCELLED), each releasing
scholarship money first (proportional reversal) and then payment money (last-in-first-out) so
no line stays over-covered (BR-WDR-03). Refunds are never automatic: the exit returns a refund
quote, and a WITHDRAWAL / CANCELLATION refund is requested separately. Phase 6 adds
reconciliation: balance snapshots (the reported balance, checked against the ledger) and a
detector that runs every ARCHITECTURE §12.1 check (structural, relationship, temporal,
lifecycle, arithmetic, the five required anomaly types, and the per-student balance identity
BR-BAL-02), re-checks every service-layer rule of the design
([`docs/phase6_sv_coverage.md`](docs/phase6_sv_coverage.md)), and stores what it finds as
findings, without ever changing domain data. Phase 7 adds the synthetic data generator
(ARCHITECTURE §11): a deterministic in-memory pipeline that builds the starter-scale dataset
with the domain calculators (identical, row for row, to replaying the same operations through
the services), injects the five required anomaly types with ground-truth labels kept in their
own database, runs the blind detector and evaluates it (precision / recall / F1 per type).
Phase 8 validates the full scale (10,000 students, about 1.25 million rows) and adds
`export_data` (CSV, JSON and SQL with checksummed manifests), `verify_export` (validation of an
export from its files alone; there is no import) and `verify_reproducibility` (the same seed in
two isolated processes, compared file by file). Changes by phase: [`CHANGELOG.md`](CHANGELOG.md).

Tested on Python 3.14.6 and Python 3.12 with the pinned requirements (Django 5.2.19).

**Unresolved design limitations:** learner "own record" and instructor "own programs" access
(ARCHITECTURE §13) are **blocked**, because the frozen model has no login↔student or
login↔program link. Learners currently have no access; instructors read programs and terms only.
Billing, scholarship, payment and refund data (Phases 2–5) is readable by `platform_admin` and
`support_staff` only; support staff may also request a refund. Withdrawals and cancellations
may be performed by `platform_admin` and `academic_admin`; only `platform_admin` sees their
financial details. Reconciliation runs and snapshots are `platform_admin` only; runs, findings
and snapshots are readable by `platform_admin` and `support_staff`. Data generation and
evaluation are `platform_admin` only.
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
| GET, POST | `/api/v1/refunds/` | POST `{payment_id, amount, requested_date, reason?, enrollment_id?}`: reason OVERPAYMENT (default), or WITHDRAWAL / CANCELLATION with the withdrawn / cancelled `enrollment_id` (Phase 5); 422 above a cap |
| GET | `/api/v1/refunds/{id}/` | with the request-time `calculation` and the status events |
| POST | `/api/v1/refunds/{id}/approve/`, `reject/` (`{on_date}`), `process/` (`{processed_date}`) | caps re-checked at approve and process |
| GET | `/api/v1/students/{id}/refundable/` | max refundable, per-payment available amounts, the calculation |

Writes need `platform_admin`, except `POST /refunds/`, which `support_staff` may also call.
In Django Admin the payments records are read-only (no add, change or delete).
Deallocation has no endpoint (it is a service used by scholarship precedence and by
withdrawals and cancellations). A payment holds at most 99 allocation rows (`409 ALLOCATION_LIMIT_REACHED`).

### Withdrawals and cancellations API (Phase 5)

| Method | Path | Notes |
|---|---|---|
| POST | `/api/v1/enrollments/{id}/withdraw/` | `{withdrawal_date}`: on or after the term start and `enrolled_on`, on or before the term end; ENROLLED → WITHDRAWN |
| POST | `/api/v1/enrollments/{id}/cancel/` | `{cancellation_date}`: before the term start, on or after `enrolled_on`; ENROLLED → CANCELLED, invoice → CANCELLED |

Both answer `{enrollment, kind, effective_date, refund_pct, financial_details_included}`;
for `platform_admin` also `invoice` (statuses, amounts and the per-line release plan; null when
there is no non-VOID invoice) and `refund_quote`. `academic_admin` gets neither key. Everything
an exit writes (ADJUSTMENT lines and ledger entries, scholarship REVERSE rows, payment
DEALLOCATE rows, events) is append-only and happens in one transaction. Refunds afterwards:
`POST /api/v1/refunds/` with `reason` WITHDRAWAL or CANCELLATION and the `enrollment_id`.

### Reconciliation API (Phase 6)

| Method | Path | Notes |
|---|---|---|
| POST | `/api/v1/reconciliation/runs/` | `{scope: "ALL" \| "STUDENT", student_id?, as_of_date}`; synchronous; 201 with the run (COMPLETED, or FAILED with `summary.error` and no findings) |
| GET | `/api/v1/reconciliation/runs/`, `/api/v1/reconciliation/runs/{id}/` | summary: per-check counts, skipped (global) checks for a STUDENT run, findings by issue type and severity, students reconciled |
| GET | `/api/v1/anomalies/`, `/api/v1/anomalies/{id}/` | findings; filters `run`, `issue_type`, `student`, `severity` |
| POST | `/api/v1/balance-snapshots/` | `{as_of_date, student_id?}`: one student or all; an existing snapshot is returned unchanged (`created: false`) |
| GET | `/api/v1/balance-snapshots/`, `/api/v1/balance-snapshots/{id}/` | filters `student`, `as_of_date` |

Command (same engine): `python manage.py run_reconciliation --as-of 2026-12-31
[--scope STUDENT --student STU-000001]`. A run reads outside any transaction, so it is not a
snapshot: writes that overlap a run can show up as findings on the records being written;
re-run once they finish (see KNOWN_LIMITATIONS, D-R2).

### Data generation and evaluation (Phase 7)

```bash
# both databases must be freshly migrated and empty (refused otherwise, no force option)
python manage.py migrate && python manage.py migrate --database=ground_truth
python manage.py generate_data --seed 20261022 --scale starter
python manage.py evaluate --out output/starter/reports/evaluation_report.json
```

`generate_data` builds 100 students, 300 enrollments and invoices, 2,100 eligibility rows, 150
awards, 500 payments and 400 snapshots exactly (DATABASE_DESIGN §7), injects 3 anomalies of
each required type (labels in `ground_truth.sqlite3` only), runs the detector
(`triggered_by = DATAGEN`) and prints precision / recall / F1 per type. Same seed + same
`config/generation.yaml` → the same rows, labels and checksums on Python 3.12 and 3.14.

| Method | Path | Notes |
|---|---|---|
| POST | `/api/v1/datagen/runs/` | `{scale: "starter", seed?}`; synchronous; 201 with the run, its reconciliation run and the evaluation; 409 `DATASET_NOT_FRESH` unless both databases are empty |
| GET | `/api/v1/datagen/runs/`, `/api/v1/datagen/runs/{id}/` | seed, scale, config hash, row counts, checksums (never labels) |
| GET | `/api/v1/datagen/runs/{id}/evaluation/?reconciliation_run=` | precision / recall / F1 per type, consequential and unexpected findings (COMPLETED ALL-scope runs only) |

### Full scale, export and verification (Phase 8)

Run these against databases you can afford to regenerate. `generate_data` refuses non-empty
databases; `verify_reproducibility` never opens the configured ones (it creates its own).

```bash
# generate (freshly migrated, empty databases), then export
python manage.py generate_data --seed 20261022 --scale full          # or --scale-factor 0.05
python manage.py export_data --out output/full                       # refuses a non-empty --out
python manage.py verify_export output/full                           # files only; exit 1 on failure

# the same seed twice, in two temporary isolated databases, compared byte for byte
python manage.py verify_reproducibility --seed 20261022 --scale starter
python manage.py verify_reproducibility --scale full --workdir /tmp/sf-repro-full --keep

# an alternative generation config (validated; every problem is listed)
python manage.py generate_data --config my_generation.yaml
python manage.py export_data --out output/custom --config my_generation.yaml
```

`export_data` writes `dataset/{csv,json,sql}` (22 tables; `schema.sql` + `seed.sql` load into
an empty SQLite file), `ground_truth/` (labels, kept apart from the dataset), `reports/`
(`findings.csv`, `evaluation_report.json`, `validation_summary.md`), `source_manifest.json`,
`dataset_manifest.json` (sha256 of every file and a combined digest; no timestamps) and
`run_log.json` (times, host, git SHA: the only file that differs between identical runs). It
refuses data changed after generation, a different config or code (the manifest would be
false) and a missing reconciliation run. Full scale on a 2-core, 8 GB machine: about 3 minutes
to generate and reconcile, 2 minutes to export, peak memory about 4.4 GB; see
[`docs/phase8_verification_results.txt`](docs/phase8_verification_results.txt).

## Tests and quality

Everything implemented so far (Phases 0–8), including every unit test:

```bash
.venv/bin/python -m pytest tests/core tests/config tests/architecture tests/models tests/services \
    tests/api tests/commands tests/verification tests/workflow tests/datagen \
    tests/unit/test_invoice_status.py tests/unit/test_invoice_lifecycle.py \
    tests/unit/test_fee_schedule_versions.py tests/unit/test_billing_calculators.py \
    tests/unit/test_full_ride_scholarship.py tests/unit/test_scholarship_calculators.py \
    tests/unit/test_payment_allocation.py tests/unit/test_payment_calculators.py \
    tests/unit/test_withdrawal_calculators.py tests/unit/test_withdrawal_refund_pct.py \
    tests/unit/test_withdrawal_release.py tests/unit/test_worked_example.py \
    tests/unit/test_reconciliation_checks.py tests/unit/test_reconciliation_sv_rules.py \
    tests/unit/test_reconciliation_counter_gaps.py tests/unit/test_stage_rng.py \
    tests/unit/test_evaluation_metrics.py tests/unit/test_datagen_config.py \
    tests/unit/test_generation_config_validation.py -rxX
#   2698 passed, 3 skipped (opt-in full scale), 2 xfailed (the two blocked rules)
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

Full-scale tests are opt-in (about 12 minutes, up to ~4.4 GB): `SF_FULLSCALE=1 .venv/bin/python
-m pytest tests/datagen/test_scales.py tests/datagen/test_fullscale.py`.

`tests/unit/test_stage_rng.py`, the acceptance test for the data generator (ARCHITECTURE
§14), now runs with the rest. Run the suite on one interpreter at a time: two concurrent runs
share the SQLite test database files.

`tests/verification/` is the API verification suite: every endpoint and method, roles,
ground-truth isolation, health outages, rollback, determinism, migration safety, concurrency,
and every response validated against the OpenAPI schema. Results:
[`docs/phase1_verification_results.txt`](docs/phase1_verification_results.txt),
[`docs/phase2_verification_results.txt`](docs/phase2_verification_results.txt),
[`docs/phase3_verification_results.txt`](docs/phase3_verification_results.txt),
[`docs/phase4_verification_results.txt`](docs/phase4_verification_results.txt),
[`docs/phase5_verification_results.txt`](docs/phase5_verification_results.txt),
[`docs/phase6_verification_results.txt`](docs/phase6_verification_results.txt),
[`docs/phase7_verification_results.txt`](docs/phase7_verification_results.txt),
[`docs/phase8_verification_results.txt`](docs/phase8_verification_results.txt).

## Layout

```
config/          settings (two SQLite DBs), GroundTruthRouter, URLs
core/            IDs, money (Decimal), domain errors + API error envelope, permissions,
                 SQLite system checks, IdSequence, StatusEventBase, health view
students/ academics/   Phase 1 apps; academics also: calculators.py (BR-WDR-02 schedule),
                 exits.py (exit-handler registry), withdraw / cancel (Phase 5)
billing/         Phase 2: models, calculators (pure), lifecycle, policy, services, API;
                 allocations.py = registry through which higher apps' allocations are seen;
                 withdrawals.py = the financial side of an exit (Phase 5)
scholarships/    Phase 3: models, calculators (pure), lifecycle, policy, services, API,
                 seed_scholarship_types command
payments/        Phase 4: models, calculators (pure), lifecycle, policy, services, API,
                 read-only admin
reconciliation/  Phase 6: models, calculators (pure; reuse the domain calculators), checks/
                 (one module per §12.1 family), dataset + loader (default DB only), engine,
                 services, API, read-only admin, run_reconciliation command
datagen/         Phase 7: rng (stage_rng), config, pipeline/ (engine = the services' steps on
                 in-memory records, scenario = stages 1–13, injector, materialize = stage 17,
                 validation), services (generate), evaluation, digest, API, generate_data and
                 evaluate commands; config/generation.yaml. Phase 8: config_schema, provenance,
                 export, verification, reproducibility; export_data, verify_export and
                 verify_reproducibility commands
tests/           core/, config/ (Phase 0); models/, services/, api/, commands/, verification/
                 (Phases 1–6); workflow/ (§9.3 steps 1–11, step 11 on fixtures); architecture/;
                 unit/ (acceptance tests, all runnable); datagen/ (Phases 7–8); billing_fixtures.py,
                 scholarship_fixtures.py, payment_fixtures.py, reconciliation_fixtures.py (a clean
                 world built through the services, and a test-only anomaly injector),
                 datagen_replay.py (the parity replay through the services)
docs/            design documents (frozen v1.1), KNOWN_LIMITATIONS.md, verification results
```
