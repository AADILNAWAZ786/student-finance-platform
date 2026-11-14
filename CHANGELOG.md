# Changelog

All notable changes to the Student Finance backend, by delivery phase. Design documents
(`docs/ARCHITECTURE.md`, `docs/DATABASE_DESIGN.md` v1.1) are frozen and unchanged throughout.
Policy decisions, observations and defects of each phase are in
[`docs/KNOWN_LIMITATIONS.md`](docs/KNOWN_LIMITATIONS.md); per-phase test evidence is in
`docs/phase*_verification_results.txt`.

## Phase 8: full-scale tooling (branch `phase8-fullscale-tooling`, not yet merged)

Base: `main` at `f31635f`. No migrations, no new dependency, no change to the frozen documents.

### Added
- `export_data` command (`datagen/export.py`): the ARCHITECTURE §11.4 layout:
  `dataset/{csv,json,sql}` for the 22 dataset tables in primary-key order, `ground_truth/`,
  `reports/` (`findings.csv`, `evaluation_report.json`, `validation_summary.md`),
  `source_manifest.json`, a deterministic `dataset_manifest.json` (sha256, bytes and rows per
  file, combined digest, row counts, coverage; no timestamps), and `run_log.json` (execution
  metadata only, never checksummed). `schema.sql` + `seed.sql` load into an empty SQLite
  database. A safety preflight refuses modified data, config or code drift, a missing
  reconciliation run and a non-empty output directory; output is moved into place only when
  complete.
- `verify_export` command (`datagen/verification.py`): validation only, from the files alone,
  with no database import: manifest hashes, CSV = JSON = SQL value by value, IDs, foreign
  keys, ground-truth labels and their separation from `dataset/`. Exits non-zero on failure.
- `verify_reproducibility` command (`datagen/reproducibility.py`): two sequential subprocess
  runs (migrate, generate, export), each with its own settings module and fresh SQLite files
  in a temporary directory and a different `PYTHONHASHSEED`; compares every export file except
  `run_log.json`; names the first differing file, primary key, column and values; writes
  `reproducibility_report.md`.
- `--config PATH` for `generate_data`, with full validation of the generation config
  (`datagen/config_schema.py`): every problem listed with its path.
- `GenerationRun.code_version` is set to the git SHA (`datagen/provenance.py`).
- Opt-in full-scale tests (`SF_FULLSCALE=1`): `tests/datagen/test_scales.py` (half and full
  plans) and `tests/datagen/test_fullscale.py` (full-scale reproducibility + `verify_export`).
- This changelog; `docs/phase8_verification_results.txt`.

### Fixed
- P8-B1 to P8-B3: the full scale failed its own validation (boundary cases planted or counted
  once instead of per occurrence / scaled). Every factor 0.02–1.0 now validates; the starter
  output and both Phase 7 golden checksums are unchanged.
- P8-B5: the Phase 7 datagen test fixtures could flush the configured databases when a test
  module lacked a `django_db` mark; they now refuse unless connected to the test databases.

## Phase 7: data generation and evaluation

- Deterministic in-memory generator (ARCHITECTURE §11): stages 1–13 on in-memory records
  through the domain calculators, five injected anomaly types with ground-truth labels in a
  separate database, stage-17 ID materialization; identical, row for row, to replaying the
  same operations through the services (parity test).
- `generate_data`, `evaluate`, `POST/GET /api/v1/datagen/runs/…`; precision / recall / F1 per
  type; canonical SHA-256 checksums with pinned goldens on Python 3.14 and 3.12.
- `DATASET_NOT_FRESH` (409) extension error code.

## Phase 6: reconciliation

- Balance snapshots; a detector running every ARCHITECTURE §12.1 check and the five required
  anomaly types, re-checking every service-layer rule (`docs/phase6_sv_coverage.md`), storing
  findings without changing domain data; ID counter-gap detection; `run_reconciliation`.

## Phase 5: withdrawals and cancellations

- Withdrawals on the BR-WDR-02 schedule and cancellations before term start (BR-WDR-01),
  releasing scholarship money first and payment money last-in-first-out (BR-WDR-03); refund
  quotes, with WITHDRAWAL / CANCELLATION refunds requested separately.

## Phase 4: payments and refunds

- Payments from any payer, AUTO and MANUAL allocation as append-only rows, scholarship
  precedence, unallocated credit; refunds (request → approve → process / reject) capped by net
  paid, credit and the source payment's available amount; `ALLOCATION_LIMIT_REACHED`
  extension code; Payment and Refund immutability, read-only admin.

## Phase 3: scholarships

- Scholarship types, eligibility evaluation, awards and their application to invoice lines;
  full-ride rules; `seed_scholarship_types`.

## Phase 2: billing

- Fee schedules and versions, invoices and lines, waivers, statuses, void, late fees, the
  ledger, and the student ledger, statement and balance views.

## Phase 1: students and academics

- Students, programs, terms and enrollments with status events; role-based permissions;
  the API verification suite (`tests/verification/`).

## Phase 0: foundation

- Project skeleton: two SQLite databases with the ground-truth router, ID sequences, money
  as Decimal, domain errors and the API error envelope, permissions, health view, OpenAPI.
