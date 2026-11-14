# Known limitations (unresolved) and Phase 2–8 policy decisions

Status (2026-11-14): Phases 0–8 are merged into `main` (Phase 8 through pull request #1, merge
commit `ac3759f`); Phase 9 is in progress on branch `phase9-demo-documentation`. L1 and L2 are
**not implemented**.
The frozen design documents (`ARCHITECTURE.md`, `DATABASE_DESIGN.md` v1.1) are unchanged;
resolving L1/L2 needs a design decision by the project owner.

## L1: Learner "read own record" (ARCHITECTURE §13): BLOCKED

- **Design says:** a learner can read their own student record, statement and balance.
- **Why blocked:** the frozen data model has no link between a login (`auth.User`) and a
  `Student`, so the API cannot tell which student record belongs to a learner.
- **Current behaviour (tested, passing):** users in the `learner` group get
  `403 PERMISSION_DENIED` on every students, programs, terms and enrollments endpoint, and
  (Phases 2–6) on every billing, scholarship, payment, refund and reconciliation endpoint
  and on withdraw / cancel, including
  `/students/{id}/statement/`, `/balance/` and `/refundable/`.
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

## Phase 8 policy decisions (approved before implementation)

| # | Decision | Implemented as |
|---|---|---|
| P8-D1 | Fix the full-scale boundary defects, keeping the starter goldens | P8-B1 to P8-B3 below; `boundary_quota(f) = max(1, round(5 × f))` equals the Phase 7 behaviour at the starter factor, so the dataset, label and config checksums are unchanged (pinned tests). |
| P8-D2 | `export_data` with the ARCHITECTURE §11.4 layout | `datagen/export.py`. 22 dataset tables (the materialized models minus `IdSequence`, an internal counter) as CSV, JSON and SQL in primary-key order, LF only; `ground_truth/`, `reports/`, `source_manifest.json`, `dataset_manifest.json`, `run_log.json`. Written to a temporary sibling directory and moved into place only when complete. |
| P8-D3 | Export safety preflight | Refuses: no or several generation runs; data changed since generation (lists the tables whose checksum changed); a config whose hash differs (`--config` names the original); code drift (a fresh in-memory re-plan gives different checksums); a changed label checksum; no COMPLETED ALL-scope reconciliation run as of 2026-12-31; a non-empty output directory. |
| P8-D4 | `verify_reproducibility` | Two sequential subprocess runs with generated settings modules and fresh SQLite files under the work directory; a settings probe refuses database paths outside the run directory or already existing, before migrating; `PYTHONHASHSEED` 1 and 2. Compares every export file except `run_log.json`. |
| P8-D5 | `verify_export`, validation only, **no database import** | `datagen/verification.py`. Ten checks (manifest, files, row_counts, csv_json, csv_sql, ids, foreign_keys, labels, separation, reports). SQL is loaded into a temporary SQLite file only; no query reaches a Django database (tested). |
| P8-D6 | Config validation and `--config` | `datagen/config_schema.py`; every problem listed with its path; `generate_data --config`, recorded through `config_hash`. |
| P8-D7 | Full-scale database tests are opt-in | `SF_FULLSCALE=1` (`tests/datagen/test_scales.py`, `tests/datagen/test_fullscale.py`); sequential, in temporary databases. |
| P8-D8 | `code_version` = git SHA | `datagen/provenance.py`; stored in `GenerationRun.code_version`, reported in `run_log.json` and the reproducibility report, never checksummed. |
| P8-D9 | Documentation | `CHANGELOG.md`, `docs/phase8_verification_results.txt`, this section, README. |

## Phase 8 observations

| # | Observation | Current behaviour |
|---|---|---|
| P8-O1 | The exported `schema.sql` omits `recorded_at` | `recorded_at` is execution metadata excluded from every checksum (D-G2, P7-O5) and from the exported rows; it is `NOT NULL` without a database default, so `seed.sql` could not load if the column stayed. The exported schema is therefore the dataset's schema, not a byte copy of the Django tables; tables come first, then their indexes. |
| P8-O2 | What "deterministic" covers | Every export file but `run_log.json`: dataset files, ground truth, reports, `source_manifest.json` and `dataset_manifest.json` (byte for byte; it has no timestamps, host, path or git SHA). Generation and reconciliation run IDs are part of it; they are deterministic because generation requires fresh databases (D-G6). Starter export golden: `combined_sha256 = 229335b4…8db1` on Python 3.14.6 and 3.12.3. |
| P8-O3 | Preflight cost at full scale | The code-drift check re-plans the dataset in memory and compares checksums: about 80 s of a 127 s full-scale export on a 2-core machine. Accepted: it is what proves the manifest describes this code and config. |
| P8-O4 | `verify_export` memory | It holds the primary keys of all tables (for foreign keys and label references) and one table's CSV and JSON rows at a time. Measured at full scale in `docs/phase8_verification_results.txt`. |
| P8-O5 | `run_log.json` paths | `output` is the resolved path, so through a symlinked directory it names the real one. |
| P8-O6 | `code_version` does not record a dirty working tree | It is `git rev-parse HEAD`; uncommitted changes are not detected. The export's code-drift check (P8-D3) is what guarantees that the exporting code reproduces the data. |
| P8-O7 | No import command | As instructed, there is no `import_data`. `schema.sql` + `seed.sql` can be loaded into a new SQLite file by hand for inspection; they are not a migration path into the application database. |

## Defects found and fixed during Phase 8 implementation (before delivery)

| # | Defect | Fixed in |
|---|---|---|
| P8-B1 | At scale factors ≥ 0.5 the Phase 7 validation rejected its own dataset: the late-fee minimum and maximum boundary cases were counted once if present (the delivered full-scale dataset has 951 and 147), against a scaled minimum. | `datagen/pipeline/validation.py`: counted per occurrence. |
| P8-B2 | The ₹0.01 overpayment boundary case was planted once at every scale. | `datagen/pipeline/scenario.py`: planted `boundary_quota(f)` times. |
| P8-B3 | Each withdrawal boundary day (0, 7, 8, 14, 15, 27, 28) was planted once at every scale. | `scenario.py`: planted `boundary_quota(f)` times each. |
| P8-B4 | The first `schema.sql` ordered statements by name only, so some indexes preceded their tables and the file did not load. | `export.py`: tables first, then indexes. Found by the export tests; `verify_export` rejects such a file (`csv_sql`). |
| P8-B5 | **Test safety.** The Phase 7 module fixtures (`tests/datagen/conftest.py`) unblock database access themselves; in a module without a `django_db` mark for both databases the test databases are never created, and the fixtures flushed and generated into the **configured** database paths. During Phase 8 development this created two empty files (no tables) at `<repo>/db.sqlite3` and `<repo>/ground_truth.sqlite3`; nothing existed there, the owner's databases (kept elsewhere) are unchanged, and the empty files were moved out of the checkout. | `_flush` now refuses unless every connection is its configured TEST database (`assert_test_databases`); `tests/datagen/test_fixture_guard.py`. *(Correction, Phase 9: the two side files of the empty `db.sqlite3`, `db.sqlite3-wal` (0 bytes) and `db.sqlite3-shm`, were not moved at the time and stayed in the checkout; they were found and moved out at the start of Phase 9. They held no data; nothing has opened that path since.)* |

## Phase 7 policy decisions (approved before implementation)

| # | Decision | Implemented as |
|---|---|---|
| D-G1 | Generation engine | The frozen design (ARCHITECTURE §5, §11.2): `datagen/pipeline/engine.py` mirrors each Phase 1–5 service step by step on in-memory records, using only the domain calculators and lifecycle tables; temporary keys; stage-17 ID materialization; `bulk_create`. Parity: `tests/datagen/test_parity.py` replays all 2,685 baseline operations through the real services and gets identical tables, row for row, IDs included (23 tables). |
| D-G2 | Checksums and cross-version determinism | Canonical SHA-256 per table (rows as sorted-key JSON lines, ordered by PK, decimals with their field's places) excluding only `recorded_at` and run `started_at` / `finished_at`; a combined dataset digest; a separate label checksum. Randomness uses only `random()` / `randrange()` / `randint()` of `stage_rng`; distributions are built from them. Pinned golden digests pass on Python 3.14.6 and 3.12.3 and under four `PYTHONHASHSEED` values. |
| D-G3 | Ground-truth label IDs | `GT-2026-NNNNNN`, numbered in memory in (issue type, record ID) order; no `GT` key in `core.IdSequence` (tested). |
| D-G4 | Consequential findings | A finding of another type than a label, on a record in that label's `related_ids`, is consequential: counted separately, never a false positive (ARCHITECTURE §12.3). The three per starter run are each injected refund's source payment (P6-O3). |
| D-G5 | Cross-database commit order | Domain rows are written in a `default` transaction; the labels in a `ground_truth` transaction nested inside it, which commits first. See P7-O3 for both failure directions and recovery. |
| D-G6 | Fresh databases only | `generate_data` and `POST /datagen/runs/` refuse (409 `DATASET_NOT_FRESH`, command error) unless every domain, run and label table is empty. No force option. |

## Phase 7 observations

| # | Observation | Current behaviour |
|---|---|---|
| P7-O1 | Phase 7 / Phase 8 boundary (ARCHITECTURE §16) | Phase 7 delivers and verifies the starter scale. `--scale full` and `--scale-factor` run the same code but full-scale volumes, timing, `export_data`, file manifests, `verify_reproducibility`, `run_log.json` and `validation_summary.md` are Phase 8; `demo_workflow`, DATA_DICTIONARY and API.md are Phase 9. |
| P7-O2 | `DATASET_NOT_FRESH` is a second approved extension to the 18 frozen domain error codes (beside Phase 4's `ALLOCATION_LIMIT_REACHED`) | 409; registered in `core.exceptions.EXTENSION_DOMAIN_ERROR_CODES` (the only Phase 0–6 module changed besides the `config/urls.py` include). |
| P7-O3 | D-G5: SQLite cannot commit two databases atomically | Labels commit first. (1) A failure before or while writing the labels rolls both back: nothing is written. (2) A failure of the domain commit after the labels committed leaves labels only; the next generation is refused (the ground-truth database is not fresh), so a dataset can never be mixed with another run's labels. Recovery: restore **both** database backups taken before generating (or delete both files and migrate both again); both are then in the same pre-generation state and generation can run. Both directions are tested (`tests/datagen/test_transactions.py`). |
| P7-O4 | The label checksum is not stored in `GenerationRun` | `GenerationRun` lives in the default database, which the detector reads; the label checksum is computed from the ground-truth database and reported by `generate_data`, `evaluate` and the evaluation endpoint. `GenerationRun.row_counts` / `checksums` name no label table (tested). |
| P7-O5 | One `recorded_at` per generation | Every generated row and label gets the run's start time, so insertion timing cannot reveal injected rows (`recorded_at` is excluded from checksums anyway). |
| P7-O6 | BR-ALC-06 (scholarship precedence) is not exercised by the generated baseline | Awards are made and applied on the invoice date, before any payment. If the engine ever met a release it raises rather than approximate; the rule stays covered by the Phase 3–4 tests. |
| P7-O7 | Generation policies the design leaves open (assumptions, all in `config/generation.yaml`) | 20 named programs (8 STEM, 12 with a lab) priced ₹3,000–₹12,000 per credit; flat fees (REGISTRATION 5,000, TECHNOLOGY 4,000, LIBRARY 2,000, STUDENT_ACTIVITY 3,000, LAB 12,000, INTERNATIONAL_SERVICE 15,000); international ×1.6 and FALL +4 % on tuition; 10 FALL tuition V2 versions at +3 %; students created on 2026-01-01, admitted 25 % in TERM-2026-SPRING and the rest in 2023–2025; 1.5 % designated full-ride students (GPA 3.85–4.00, need 0.85–1.00; no other student may qualify); every enrollment on or before the invoice date; payment profiles (full, instalments, partial, late) with exact quotas; 2.5 % of students go ON_HOLD and back; refunds requested after exits and selected overpayments; a refund whose caps no longer hold when it is approved or processed is REJECTED (as an operator would after the 422). |
| P7-O8 | Starter-scale targets for the count-based bands (DATABASE_DESIGN §7 states full scale) | Scaled by the factor, never below 1: full-ride awards 3–6, `SCHOLARSHIP_COVERED` invoices ≥ 3, each boundary case ≥ 1. Percentage bands apply unchanged. Generation fails when any is missed. |
| P7-O9 | The "≈" volumes of DATABASE_DESIGN §7 are outcomes, not targets | Starter actuals: InvoiceLine 1,767 + 17 injected (≈ 1,800), LedgerEntry 2,476 + 20 (≈ 2,520), InvoiceEvent 1,086 + 6 (≈ 750: a COLLECTION event is written at every status change, as the services do), PaymentAllocation 1,892 + 3 (≈ 1,280: instalments and AUTO allocation across lines), Refund 22 + 3 (≈ 24). |
| P7-O10 | Rolling back `datagen.0001_initial` | Drops `GenerationRun` and the label table; generated domain rows stay, so the next generation is refused. A full rollback of a generated dataset is a restore of both database backups. |
| P7-O11 | `meets_targets` with a type that has neither labels nor findings | Vacuously met (0/0 is reported as null, never as 1); a type with labels and no finding fails through recall 0. |
| P7-O12 | Existing tests extended for Phase 7 (no assertion removed or weakened) | `tests/config/test_router.py` and `tests/verification/test_verify_isolation.py` (the ground-truth database now holds exactly the label table; every model but GroundTruthLabel routes to default); `tests/verification/test_verify_withdrawals.py::test_phase5_needs_no_migration` (also allows the ground-truth connection, which `makemigrations --check` now inspects); `tests/verification/test_verify_routes.py` (48 → 51 routes); `tests/verification/test_verify_openapi_examples.py` (+15 examples, all replayed); `tests/core/test_exceptions.py` (P7-O2). |

## Defects found and fixed during Phase 7 implementation (before delivery)

| # | Defect | Fixed in |
|---|---|---|
| P7-B1 | The first scenario draft let a single-payment profile receive extra payment slots from the exact-count adjustment; the extra payments found nothing open and became 33 advance payments. | `datagen/pipeline/scenario.py`: every payment but the last on an invoice pays a share of what is open. Found by the scenario statistics before any test existed; now 0 advance payments. |
| P7-B2 | The "scholarship exactly at cap" boundary counter also counted FIXED awards (always equal to their cap). | Counts only PERCENT awards capped exactly. |

## Phase 6 policy decisions (approved before implementation)

| # | Decision | Implemented as |
|---|---|---|
| D-R1 | Run persistence (**Option A**) | The run is stored RUNNING in its own short transaction; the checks run outside any transaction; every finding and COMPLETED (summary, finished_at) are written in one transaction. On any failure that transaction rolls back (no partial findings, finding IDs released) and the run is set FAILED with `summary.error` in its own transaction. Transitions in `reconciliation/lifecycle.py`: ∅→RUNNING→COMPLETED / FAILED, both terminal. The API answers 201 with the run in either end state; the command exits non-zero on FAILED. |
| D-R2 | Read consistency | Not a snapshot, and not claimed to be one: a write committed while a run reads may be seen by some checks and not by others, so a run overlapping writes can report findings on the records being written. Re-run once the writes finish. Concurrency tests show such findings only ever concern the student being written, and a later run is clean. |
| D-R3 | BR-BAL-02 failures | `ARITHMETIC_MISMATCH`, rule BR-BAL-02, entity Student, record_id = student_id; the student is listed in `summary.students.not_reconciled`. |
| D-R4 | REFUND_EXCEEDS_NET_PAID | Phase 4's own calculation (`payments.calculators.net_paid`), replayed per student in request order (requested_date, refund_id): a refund not REJECTED breaches when it exceeds the student's payments less every earlier refund not REJECTED. REJECTED refunds reserve nothing. See P6-O2 for "not justified by unrelated payments". |
| D-R5 | SCHOLARSHIP_EXCEEDS_ELIGIBLE_FEES | Phase 3's own `eligible_fee_base` and its `reserved_on_invoice` rule (awarded amount while OFFERED / ACCEPTED, net applied afterwards, nothing once REVOKED), per invoice in (award_date, scholarship_id) order: an award breaches when its awarded amount exceeds its type's base, or the reservations up to and including it exceed that base. Parity with `reserved_on_invoice` is tested. Rule BR-SCH-08 for the full-ride type, BR-SCH-04 otherwise. |
| D-R6 | Existing snapshot | Returned unchanged with `created = false` (no new error code); 201 when at least one snapshot was created, 200 otherwise. |
| D-R7 | Collection-status recompute | Exactly as the services derive it: BR-INV-06 from all of the invoice's rows, at its `collection_status_as_of`. Invoices whose `collection_status_as_of` is after the run's as-of date are not evaluated (counted in `summary.invoices_collection_status_not_evaluable`). |
| D-R8 | rule_id without a BR rule | Frozen requirement IDs: R-22 (orphans), R-26 (joins), R-28 (ID format), R-39 (enums, duplicates, date order), R-48 (lifecycle chains). |
| D-R9 | Severity | CRITICAL: PAYMENT_OVER_ALLOCATED, REFUND_EXCEEDS_NET_PAID. HIGH: DUPLICATE_INVOICE, SCHOLARSHIP_EXCEEDS_ELIGIBLE_FEES, BALANCE_LEDGER_MISMATCH, ARITHMETIC_MISMATCH, ORPHAN_FK, DUPLICATE_RECORD. MEDIUM: RELATIONSHIP_MISMATCH, INVOICE_STATUS_MISMATCH, ILLEGAL_TRANSITION, DATE_ORDER, INVALID_ENUM. LOW: INVALID_ID_FORMAT. Recommended action per DATABASE_DESIGN §7 for the five required types, INVESTIGATE otherwise. |
| D-R10 | STUDENT scope | Runs the checks that can be tied to one student; the eight GLOBAL checks (S-FK-ORPHAN, S-ID-MASTER, S-ID-GAPS-YEAR, S-ENUM-MASTER, S-DUP-MASTER, T-FEE-CHAIN, T-TERM-CENSUS, RL-BILLABLE-TERM-MASTER) are listed in `summary.skipped_checks`. |
| D-R11 | Extra read endpoints | `GET /balance-snapshots/`, `/balance-snapshots/{id}/`, `/anomalies/{id}/`, read-only, same roles as the other reconciliation reads. |
| D-R12 | Command | `python manage.py run_reconciliation --as-of YYYY-MM-DD [--scope STUDENT --student STU-…]`, the same service as the API, `triggered_by = COMMAND`. |
| D-R13 | IDs | Pattern and rebuild through `core.ids`: parent segments (invoice student number, line invoice, allocation payment / award, fee-schedule natural key) and the business year the services pass to `next_id`. Counter gaps: originally not findings unless a frozen requirement explicitly demands them; you confirmed DATABASE_DESIGN §2 and ARCHITECTURE §12.1 as that requirement (P6-O15), so gaps are now checked within each `core.ids` sequence scope (S-ID-GAPS-PARENT, S-ID-GAPS-YEAR). |

## Phase 6 observations

| # | Observation | Current behaviour |
|---|---|---|
| P6-O1 | **Conflict with Phase 2 behaviour (R-45):** "line unit = version in force" cannot be re-checked against the current fee chain: Phase 2 allows adding a fee version effective on or before a term's invoice_date after invoices were issued from the previous one, and nothing records which versions existed at issue. | A-FEE-PRICE checks only what is unambiguous: the line's own schedule was in force by the invoice date (effective_from ≤ invoice_date), its unit is that schedule's amount, and its quantity follows the charge basis. No replacement rule was invented. Kept, as approved, as a historical fee-version limitation: no historical data is invented and the frozen design is unchanged (a stricter check would need the pricing version set recorded at issue). |
| P6-O2 | D-R4 "refunds on one payment must not be justified by unrelated payments": BR-REF-01 (frozen) is student-level net paid, so it cannot express this. | The per-payment limit is BR-ALC-01 (Σ allocations + Σ refunds not REJECTED ≤ amount, the frozen rule): a refund beyond its own payment's available amount makes that payment a PAYMENT_OVER_ALLOCATED finding, whatever other payments hold (tested). |
| P6-O3 | A refund above net paid necessarily also breaks BR-ALC-01 for some payment (Σ refunds > Σ payments forces one payment over its amount). | That payment gets a PAYMENT_OVER_ALLOCATED finding: the one consequential finding of the five injected types (ARCHITECTURE §12.3), asserted exactly by the anomaly tests. |
| P6-O4 | A snapshot whose balance no longer matches the ledger also has stale totals. | Reported once, as BALANCE_LEDGER_MISMATCH; totals are checked (ARITHMETIC_MISMATCH, BR-BAL-03) only on snapshots whose balance still matches (DATABASE_DESIGN §3: totals are "at snapshot time"). |
| P6-O5 | Phase 5 allows backdated exits (D-W8); a posting dated on or before an existing snapshot's date, made after it, makes that snapshot disagree with the ledger. | A true BR-BAL-03 finding (RESTATE_BALANCE), by design: snapshots are stored to be checked (P3). |
| P6-O6 | INVALID_ENUM and DUPLICATE_RECORD cannot be stored (DB CHECK and UNIQUE constraints; PK uniqueness likewise). | The checks run on every row and are tested on pure rows; they matter for imported data (Phase 7 exports, legacy loads). |
| P6-O7 | **Resolved (revised Phase 6 delta).** DATABASE_DESIGN's "every SV rule is also REC". | Every service-layer rule in DATABASE_DESIGN (SV / REC rows and service-enforced validations), ARCHITECTURE §10 and §6 is mapped in `docs/phase6_sv_coverage.md` to a check, family, rule ID, frozen issue type and test (61 rows since P6-O15; 63 checks). 23 checks were added (`reconciliation/checks/service_rules.py`), among them BR-SCH-03 (A-SCHOLARSHIP-CAP), BR-SCH-05/08 (S-SCHOLARSHIP-GROUP) and BR-LATE-01 (A-LATE-FEE). No issue type added; D-R4 and D-R5 unchanged. `tests/architecture/test_sv_coverage_matrix.py` keeps the matrix true to the code. The parts no stored data can re-check are listed in its section D (P6-O1, P6-O13, P6-O14). |
| P6-O8 | OpenAPI: an as_of_date outside 2026 is a 400 on both POST endpoints with exactly the billing endpoints' body. | Not repeated as an example on the reconciliation endpoints (the harness forbids sharing a specific example across resources); the behaviour is tested in tests/api/test_api_reconciliation.py. |
| P6-O9 | ILLEGAL_TRANSITION also covers "stored status ≠ last event" for students, enrollments, scholarships and refunds (for invoices: INVOICE_STATUS_MISMATCH). | DATABASE_DESIGN §3 has no separate issue type for it; `details.problem` = STORED_STATUS_NOT_LAST_EVENT or NO_EVENTS. |
| P6-O10 | Event chains are replayed in event_id (creation) order; an invoice has two chains, LIFECYCLE and COLLECTION (D13). | Event dates must not go back within a chain (DATE_ORDER, R-39). |
| P6-O11 | Performance: a run loads the rows of its scope into memory once and calls the pure calculators; no per-row queries. | Measured on test-sized data only; full-scale timing belongs to Phase 8. |
| P6-O12 | In a STUDENT-scope run, rows of other students are not loaded. | A join to another student's row (e.g. a refund on another student's payment) is reported by the ALL-scope run. |
| P6-O13 | BR-LATE-01's exact base (2 % of what was outstanding at the end of the grace day) cannot be re-computed: Phase 5 allows exits dated before that day after the fee was posted (D-W8), which legitimately changes the as-of outstanding later. | A-LATE-FEE re-checks the stable parts: once per invoice, the posting date, the invoice still ISSUED then, ₹500–₹5,000, and no more than 2 % (clamped) of the charges posted by the grace cutoff (an upper bound, since outstanding ≤ charges). |
| P6-O14 | Choices the services make on the state at that moment cannot be re-checked from stored data: AUTO allocation order (BR-ALC-03), the LIFO release choice (BR-ALC-06, BR-WDR-03), the scholarship priority order (BR-SCH-06), the proportional reversal split (BR-SCH-07 / BR-WDR-03), "applied on or after the invoice's latest posting or status date", and Eligibility.eligible_fee_base (taken from the invoice that existed at evaluation). | Their final-state consequences are re-checked: line open ≥ 0, releases ≤ originals, the exit adjustments themselves (A-EXIT-ADJUSTMENTS), award caps, BR-BAL-02. Exact re-checking would need the moment-of-decision state recorded, a design change. |
| P6-O15 | **Resolved.** DATABASE_DESIGN §2 ("Reconciliation checks format, counter gaps and duplicates") and ARCHITECTURE §12.1 ("ID counter gaps/duplicates") were confirmed as the explicit requirement D-R13 asked for. | Gaps are reported as INVALID_ID_FORMAT (R-28, LOW / INVESTIGATE; no issue type added), one finding per run of missing counters, on the first missing ID, with `details.problem = COUNTER_GAP`, the sequence, the run and the next present ID. Scopes are exactly `core.ids.sequence_key`: global (STU, PRG, SCT); per year (ENR by term year, ELG, SCH, PAY, REF, LED, SNAP; EVT per entity and year) in S-ID-GAPS-YEAR (GLOBAL, skipped in STUDENT runs); parent-scoped (INV per student and year, LINE per invoice, SCHA per award, ALLOC per payment) in S-ID-GAPS-PARENT (PER_STUDENT). Not reported: counters above the highest stored ID (reserved but unused; the detector never reads `IdSequence`), RECON / FND / GEN (not domain data), Term / FeeSchedule / ScholarshipTypeProgram (natural keys, no counter). A misnumbered row (wrong parent or year) is reported once by S-ID-DOMAIN and fills both its own and its expected counter, so it never also produces gaps; an unparseable ID fills nothing; a sequence holding only misnumbered rows is not checked. Valid data has no gaps because every ID-allocating service is atomic and a refused operation releases its counter (verified: IdSequence equals the highest stored counter after six refused operations). Schema rollback: `migrate reconciliation zero` drops the snapshot table but `core.IdSequence` keeps the SNAP counters (counters are never reused), so after re-applying, new snapshots continue after the old highest number and one LOW finding reports the dropped range (rehearsed: SNAP-2026-000001…000090, `missing_count` 90). It reports rows that really are gone; restoring the pre-merge database backups is the full rollback and leaves no such gap. |

## Defects found and fixed during Phase 6 implementation (before delivery)

| # | Defect | Fixed in |
|---|---|---|
| P6-B1 | The ledger-completeness check passed a details key named `expected`, colliding with the finding's expected value: a mismatching ledger entry made the run FAILED instead of reporting it. | `reconciliation/checks/arithmetic.py` (details keys `expected_entry` / `actual_entry`); found by `test_identity_failure_is_an_arithmetic_mismatch_on_the_student`, which also showed the FAILED path working as designed (no partial findings). |
| P6-B2 | Two of the new service-rule checks (T-TERM-CENSUS, A-EXIT-ADJUSTMENTS) passed details keys named `expected` / `actual`, the P6-B1 defect class again: a real finding would have failed the run. | Renamed (`expected_census_date`, `expected_adjustments` / `actual_adjustments`); found by `test_census_date` and `test_exit_adjustments_that_do_not_match_the_schedule` before delivery. Every new check now has a test that drives its finding path. |
| P6-B3 | The first gap-check draft placed a misnumbered row only in its expected (canonical) counter slot, so an enrollment moved to another year left a spurious gap in its own year's sequence on top of the S-ID-DOMAIN finding, and a malformed ID got a slot from a garbage counter. | `reconciliation/checks/structural.py`: a misnumbered row fills both its own and its expected slot, an ID failing its pattern fills none, and only sequences with at least one well-numbered row are checked; found by `test_enrollment_in_a_non_billable_term` and `test_unparseable_ids_are_left_to_the_format_check` before delivery. |

## Phase 5 policy decisions (approved before implementation)

| # | Decision | Implemented as |
|---|---|---|
| D-W1 | Where withdrawals are orchestrated (**Option A**) | `academics.services.withdraw_enrollment` / `cancel_enrollment` stay the entry points. Academics runs the exit handlers registered in `academics/exits.py` (billing registers `billing.withdrawals.apply_enrollment_exit`); billing drives the scholarship reversal and the payment release through two adjustment handlers registered in `billing/allocations.py` by scholarships and payments. No new import edge; resolves D-S17 (`test_exit_orchestration_uses_registries_only`). |
| D-W2 | Reconciliation balance helper | `reconciliation.calculators.balance_from_ledger(entries, as_of=None)`, a thin wrapper around `billing.calculators.ledger_balance`. Nothing else from Phase 6. |
| D-W3 | N in BR-WDR-02/03 | The line's current net after any earlier adjustments (`billing.calculators._current_nets`). |
| D-W4 | Splitting R across several scholarship allocations on one line | Proportional to each allocation's remaining amount; largest-remainder rounding to the cent, ties by `allocation_id`; the parts sum exactly to R (`scholarships.calculators.split_reversal`). |
| D-W5 | Who may withdraw / cancel (**Option B**) | `platform_admin` and `academic_admin`. Only `platform_admin` (and superusers, P5-O4) receive `invoice` (amounts, statuses, release plan) and `refund_quote`; for an academic admin both keys are absent, not null, and `financial_details_included` is false. Everyone else: 403. |
| D-W6 | Refunds after an exit | Never automatic: the exit returns a refund quote only. `POST /refunds/` now accepts reason WITHDRAWAL / CANCELLATION with a required `enrollment_id` of the same student whose status is WITHDRAWN / CANCELLED respectively (OVERPAYMENT must omit it). The existing three caps apply unchanged; `calculation.schedule_pct` records the BR-WDR-02 fraction for WITHDRAWAL. Supersedes D-P14. |
| D-W7 | No invoice / VOID invoice | Enrollment transition only; `invoice: null` (platform admins). |
| D-W8 | Dates | Term bounds, `enrolled_on` and the existing deallocation date rule (on or after each row's `allocated_on`). No rule that the exit must follow every posting, status or allocation date. One narrow check was needed so no row predates what it adjusts: see P5-O2. |
| D-W9 | Student status, unapplied awards | Unchanged. |

## Phase 5 observations

| # | Observation | Current behaviour |
|---|---|---|
| P5-O1 | **Conflict with Phase 2 behaviour:** the frozen `tests/unit/test_worked_example.py` (runnable since Phase 5) expects `invoice_amounts(...).payments_by_payer` to list only payers with allocations; Phase 2 listed all three with zeros. | The calculator now lists present payers only. API output is unchanged: the serializers still fill 0.00 for the missing payers. |
| P5-O2 | An exit dated before an adjusted line's `posted_date` would create an ADJUSTMENT older than its own line. | 400 on `withdrawal_date` / `cancellation_date`. Reachable for a cancellation dated after `enrolled_on` but before the invoice's issue date, or an exit dated before a late fee's posting; every other date follows D-W8. |
| P5-O3 | `scholarships.services.reverse_for_adjustment` (the exit path) checks the exit date against each reversed allocation's `allocated_on`, not Phase 3's `_latest_invoice_date` rule used by the manual `reverse_allocation`. | Deliberate, per D-W8; `reverse_allocation` keeps its Phase 3 rule unchanged. |
| P5-O4 | Superusers pass every role check, so they receive financial details like `platform_admin`. | As in every earlier phase; no superuser exists unless created deliberately. |
| P5-O5 | A WITHDRAWAL / CANCELLATION refund's `requested_date` is not tied to the exit date. | The existing refund date rules apply; no new bound was approved. |
| P5-O6 | OpenAPI: the `RefundRequestReasonEnum` override (OVERPAYMENT only) was removed. | `POST /refunds/` documents `reason` with the shared `RefundReasonEnum` (all three). |
| P5-O7 | A Phase 2 unit test asserted that `release_for_adjustment` did not exist yet (D12 placeholder). | Renamed `test_release_for_adjustment_arrived_in_phase_5`, asserting it exists. |
| P5-O8 | The deferred acceptance tests `test_withdrawal_refund_pct.py`, `test_withdrawal_release.py` and `test_worked_example.py` import and pass. | D-P19 resolved; only `test_stage_rng.py` (datagen, Phase 7) remains deferred. |
| P5-O9 | The collection status is recalculated once, after every write of an exit. | Intermediate states (adjustment written, payment not yet released) would have a negative outstanding, which `check_amounts` rejects. |

## Defects found and fixed during Phase 5 implementation (before delivery)

| # | Defect | Fixed in |
|---|---|---|
| P5-B1 | The adjustment builders looked up a line's net before filtering out non-FEE / LATE_FEE lines (KeyError on an invoice with an earlier adjustment). | `billing/calculators.py`; found by `tests/unit/test_withdrawal_calculators.py`. |
| P5-B2 | An academic admin received `"invoice": null, "refund_quote": null`: DRF renders a missing `allow_null` field as null instead of skipping it. | `academics/views.py::_exit_body` drops both keys after serialization; found by `tests/api/test_api_withdrawals.py`. |

## Phase 4 policy decisions (approved before implementation, plan revision 2)

| # | Decision | Implemented as |
|---|---|---|
| D-P1 | `Payment.method` is Char(14) in the design but `SPONSOR_TRANSFER` has 16 characters | Char(16); see P4-O1. |
| D-P2 | `docs/BUSINESS_RULES.md` does not exist | The rules are taken from ARCHITECTURE §6. No file created. *(Later note, Phase 9: the catalogue now exists as `BUSINESS_RULES.md` at the repository root, at the owner's instruction.)* |
| D-P3 | Who may request a refund | `POST /refunds/`: platform_admin and support_staff (§13). Approve / process / reject, payments and allocation: platform_admin only. Everyone else (learners included): 403. |
| D-P4 | Billing needs payment and refund data without importing payments | Two student-level registry kinds in `billing/allocations.py`: `student_credit` (unallocated credit from the payment rows) and `refunds` (for statements). Payment / refund totals come from billing's own PAYMENT / REFUND ledger rows; the payer split from the `payment__payer_type` relation. |
| D-P5 | `invoice_amounts` reads `payer_type` from allocations | Read-only `PaymentAllocation.payer_type` property (the payment's). No column. |
| D-P6 | `allocated_on` | Required for MANUAL; defaults to the payment's `received_date` for AUTO and `record_payment`. A 2026 business date, on or after `received_date` and the line's posted date. |
| D-P7 | Net paid, credit, reservations (**corrected in revision 2**) | Net paid (BR-REF-01) = Σ payments − Σ refunds **not REJECTED**. Credit available = ledger credit balance (BR-REF-02: −balance if negative) − Σ REQUESTED/APPROVED refunds, ≥ 0. Unallocated credit (BR-BAL-02) subtracts PROCESSED refunds only, so the ledger identity holds while refunds are pending. |
| D-P8 | Re-checking caps | All three caps are re-checked at approval and at processing, inside the transaction, with the refund itself left out of net paid, the pending reservations and its payment's refunds. Rejection checks nothing. |
| D-P9 | `received_date` before the first invoice / no invoice (**corrected in revision 2**) | A DATABASE_DESIGN SV rule, not BR-PAY-01: 400 VALIDATION_ERROR, `rule_id: null`, field `received_date`. |
| D-P10 | Deallocation endpoint | None. `payments.services.deallocate` is a service (BR-ALC-06 now, WITHDRAWAL_/CANCELLATION_RELEASE in Phase 5). |
| D-P11 | BR-ALC-06 release | LIFO across the line's payments by (`allocated_on`, `allocation_id`) descending, `rule_applied = SCHOLARSHIP_PRECEDENCE`; freed money stays unallocated credit (not re-allocated). |
| D-P12 | AUTO scope | FEE / LATE_FEE lines, posted on or before `allocated_on`, on ISSUED or CANCELLED invoices; never VOID. |
| D-P13 | AUTO with nothing to do (**corrected in revision 2**) | No open lines: no rows, the money stays unallocated credit, 200 `outcome: NO_OPEN_LINES` (BR-ALC-04). Nothing available: no rows, 200 `outcome: NO_FUNDS_AVAILABLE`. `ALLOCATION_EXCEEDS_PAYMENT` only for a MANUAL total above the available amount. |
| D-P13a | The 99-row allocation ID limit (**Option A approved**) | New 409 domain code `ALLOCATION_LIMIT_REACHED`; see P4-O2. Checked before anything is written, counting every existing row (ALLOCATE and DEALLOCATE) plus the rows planned, in normal allocation, deallocation and the BR-ALC-06 handler. |
| D-P14 | Refund reasons | The API accepts OVERPAYMENT only. Model, CHECKs and service support all three; WITHDRAWAL / CANCELLATION (with enrollment and `schedule_pct`) are created by Phase 5. |
| D-P15 | Approval / rejection dates (**corrected in revision 2**) | Required `on_date` in the request body, stored in the inherited `StatusEventBase.event_date`. No extra column. `processed_date` is a 2026 date on or after the latest status date. |
| D-P16 | Refund source | One payment per refund. |
| D-P17 | Phase 3 tests that assumed no payments handler | Updated with the same intent (see PHASE4_VERIFY.md). |
| D-P18 | Billing placeholders | Statement payment allocations and refunds, balance payments / refunds / unallocated credit now filled; response shapes unchanged except the additive fields in P4-O3. |
| D-P19 | `tests/unit/test_worked_example.py` | Still deferred (needs Phase 5/6 modules). §7 Stages A–C are rebuilt from services in `tests/services/test_services_refunds.py`. |
| D-P20 | PaymentEvent | None (not in the design); payment effects show as InvoiceEvents and RefundEvents. |

**Implementation conditions set at approval (all met and tested):**
1. The 99-row capacity check counts all existing PaymentAllocation rows (ALLOCATE and
   DEALLOCATE) plus every row planned in the transaction, before writing, in normal
   allocation, deallocation and the BR-ALC-06 handler
   (`test_D_P13a_*` in `tests/services/test_services_payments.py`).
2. `max_refundable(net_paid, credit_balance, payment_available)` keeps its frozen signature
   (`test_max_refundable_keeps_the_frozen_signature`); the reservation figures are computed by
   separate, separately tested functions (`pending_reservations`, `credit_available`,
   `refund_caps`).
3. `Refund.calculation` is the request-time snapshot; approval and processing re-check the caps
   in their own transactions without rewriting it (`test_calculation_snapshot_is_never_rewritten`).

## Phase 4 observations

| # | Observation | Current behaviour |
|---|---|---|
| P4-O1 | **Design inconsistency:** DATABASE_DESIGN gives `Payment.method` as Char(14), but lists `SPONSOR_TRANSFER` (16 characters). | Char(16), the minimum that stores every documented value (D-P1). The design doc is unchanged; `tests/models/test_payments_schema_contract.py` allows exactly this one documented exception. **Needs owner confirmation.** |
| P4-O2 | **Approved exception to the 18-code catalogue:** `ALLOCATION_LIMIT_REACHED` (409). The frozen ID format `ALLOC-{payment_id}-NN` allows 99 rows per payment, deallocations included, and no existing code describes "no ID space left". | `core.exceptions.EXTENSION_DOMAIN_ERROR_CODES`; the 18 documented codes stay in `DOCUMENTED_DOMAIN_ERROR_CODES`, checked against ARCHITECTURE §10 by `tests/core/test_exceptions.py`. Documented on `POST /payments/{id}/allocate/` with a replayed example. `rule_id` is null (no BR rule). |
| P4-O3 | `/students/{id}/ledger/` entries gained `payment` and `refund` fields (null for other sources); `source_type` can be PAYMENT or REFUND. | Additive response change; the Phase 2 ledger test's expected dict gained the two keys. |
| P4-O4 | `Refund.calculation` keeps the four design keys (`net_paid`, `credit_balance`, `payment_available`, `schedule_pct`) plus `pending_refunds`, `credit_available` and `max_refundable`. | `credit_balance` is the BR-REF-02 ledger credit balance; the extra keys show the reservations that were subtracted from it. Money as strings. |
| P4-O5 | Allocation state (`UNALLOCATED` / `PARTIAL` / `FULL`) is not defined further by the design. | Compares net allocations with the payment amount: 0 → UNALLOCATED, = amount → FULL, otherwise PARTIAL (a partly refunded, otherwise fully used payment is PARTIAL). |
| P4-O6 | `REFUND_EXCEEDS_NET_PAID` can bind only when the credit balance exceeds net paid, which a consistent ledger reaches only with credits not yet followed by their releases (e.g. a withdrawal adjustment before the Phase 5 scholarship reversal). | Enforced and tested with such an injected adjustment (service test and replayed OpenAPI example). Net paid is always checked first. |
| P4-O7 | `GET /students/{id}/refundable/` is served by the payments app. | Billing (which serves the other student account views) may not import payments. Same URL, same permissions. |
| P4-O8 | Docstrings in `scholarships/services.py` still say no payment allocations exist (Phase 3 wording). | Behaviour is correct (BR-ALC-06 releases real allocations); the file was not in the approved Phase 4 change list, so the wording is left for the next scholarships change. |

## Defects fixed in the Phase 4 delta after review (corrective delta, before merge)

| # | Defect | Fixed in |
|---|---|---|
| P4-B1 | `Payment` refused deletion but not updates: saving a persisted payment would have rewritten it. | `Payment.save()` raises `AppendOnlyError` for any persisted payment; inserts are unchanged. |
| P4-B2 | `Refund` did not protect its financial and audit fields at model level: only the services avoided changing them. | `Refund.save()`: after insertion only `status` and `processed_date` may change (`MUTABLE_FIELDS`); a change to any of `refund_id`, `student`, `payment`, `payer_type`, `enrollment`, `reason`, `amount`, `requested_date`, `calculation`, `recorded_at` (`IMMUTABLE_FIELDS`), or `update_fields` naming one, raises `AppendOnlyError` before anything is written, and only the two mutable columns are ever written. The loaded values are deep-copied, so mutating `calculation` in place is also caught (found by the new tests). |
| P4-B3 | No Django Admin registration for the payments models (`payments/admin.py` missing; Phase 3 had one for scholarships). | `payments/admin.py` registers Payment, PaymentAllocation, Refund and RefundEvent read-only: no add, change or delete permission, even for superusers. |

`QuerySet.update()` bypasses model methods; no payments code uses it on these tables.

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
Phase 4 registered the LIFO deallocator (see the Phase 4 decisions above).

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
| P3-B2 | The API accepted `max_gpa = 0` (approved 0–4 bounds) but the DB CHECK required `max_gpa > 0`, so a valid request hit the database. | Model and `scholarships/0001_initial` (not yet merged) aligned to `0 ≤ max_gpa ≤ 4`. *(Later note: "not yet merged" describes the Phase 3 review; the corrected migration shipped in the Phase 3 commit `d8d90f2`.)* |
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
