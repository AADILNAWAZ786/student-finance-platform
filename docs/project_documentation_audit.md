# Project documentation and status audit

Date: 2026-11-14, during Phase 9 (branch `phase9-demo-documentation`, based on `main` at
`ac3759f`). Scope: every tracked documentation and configuration file (README, CHANGELOG,
`docs/**`, the root documents, `pyproject.toml`, `pytest.ini`, `config/generation.yaml`),
checked against git, the repository's files and the code. The frozen documents were read but
not changed.

## 1. Facts verified against git

| Fact | Evidence |
|---|---|
| `origin/main` = `ac3759fa91f0e1027f2e4805d67e9bb57db696ed`, "Merge pull request #1 from AADILNAWAZ786/phase8-fullscale-tooling", 2026-11-14 | `git ls-remote origin`, `git log origin/main` |
| Merge parents `f31635f` and `b170720`; tree identical to `b170720` | `git log --format=%p`, `git diff b170720 ac3759f` (empty) |
| PR #1 head = `b170720` | `refs/pull/1/head` |
| Phase 8 = `1e2d403`, `bf0f48b`, `fda5c96`, `4d7ef4b`, `408a450`, `b170720`, based on `f31635f` | `git log origin/main` |
| `f31635f` ("Merge completed project phases 5 through 7") is a **root commit** (no parent); tree `df6ae0a` = tree of `87b5810` | `git log --format=%p`, `git rev-parse <commit>^{tree}` |
| Phase 0–7 history: `a44abe4` (0–2) → `d8d90f2` (3) → `fb4c497` (4) → `d1bd07b` (5) → `efca9da` (6) → `87b5810` (7) | `git log origin/phase7-datagen` |
| Remote branches: `phase5-withdrawals` = `d1bd07b`, `phase6-reconciliation` = `efca9da`, `phase7-datagen` = `87b5810`, `phase8-fullscale-tooling` = `b170720` | `git ls-remote origin` |
| Phase 9 branch base `ac3759f`; commits `a5e15b2` (demo_workflow), `4451fbc` (data dictionary), `3b82922` (business rules); not yet published (GitHub answers 403 to this session's pushes) | `git log ac3759f..HEAD` |
| Management commands: `seed_terms`, `seed_roles`, `seed_scholarship_types`, `run_reconciliation`, `generate_data`, `evaluate`, `export_data`, `verify_export`, `verify_reproducibility`, `demo_workflow` | `*/management/commands/` |
| The fix referred to by P3-B2 (`max_gpa ≥ 0`) is in `scholarships/migrations/0001_initial.py` of `d8d90f2` | `git show` |

## 2. Findings and corrections

| # | File | Statement found | Problem | Correction made | Class |
|---|---|---|---|---|---|
| 1 | `CHANGELOG.md` | "Phase 8: full-scale tooling (branch `phase8-fullscale-tooling`, not yet merged)" | Merged through PR #1 | Heading now "merged into `main` through PR #1"; base `f31635f`, the six commits and the merge commit `ac3759f` (parents, tree) recorded | OUTDATED_CURRENT_STATUS |
| 2 | `CHANGELOG.md` | no repository history | Phase 0–7 commits, their branches and the root-commit nature of `f31635f` undocumented | "Repository history" table added | MISSING_DOCUMENTATION |
| 3 | `CHANGELOG.md` | no Phase 9 entry | Phase 9 commits exist | "Phase 9 (in progress, not merged)" entry listing only committed work and what is still to come | MISSING_DOCUMENTATION |
| 4 | `README.md` | "**Status: Phase 8, full-scale tooling**" | Phase 8 merged; Phase 9 in progress | Status: Phases 0–8 implemented and merged (PR #1, `ac3759f`); Phase 9 in progress, not merged | OUTDATED_CURRENT_STATUS |
| 5 | `README.md` | "2698 passed, 3 skipped …" as the current total; "Everything implemented so far (Phases 0–8)" | That is the Phase 8 result on `main`; the Phase 9 branch adds tests not yet regression-verified | Labelled as the Phase 8 result at `ac3759f`; Phase 9 counts deferred to its verification results | OUTDATED_CURRENT_STATUS |
| 6 | `README.md` | "Fee schedules come from the API (or test fixtures); there is no seed command for them yet" | `generate_data` (Phase 7) also creates them | Now names the API, `generate_data` and fixtures; "no separate seed command" kept (true) | OUTDATED_CURRENT_STATUS |
| 7 | `README.md` (Layout) | no Phase 9 files, root documents, `output/` | Incomplete structure | Phase 9 `demo` / `demo_workflow`, the root documents, the new architecture tests, `output/`, the root-location deviation | MISSING_DOCUMENTATION |
| 8 | `README.md` | `demo_workflow`, `DATA_DICTIONARY.md`, `BUSINESS_RULES.md` not mentioned | Committed on this branch | "Phase 9 (in progress)" section with the commands and links to files that exist; `API.md` and `PROMPT.md` named as still to come, without links | MISSING_DOCUMENTATION |
| 9 | `docs/KNOWN_LIMITATIONS.md` | "Status as of the Phase 8 (full-scale tooling) delivery" | Out of date | Status line: Phases 0–8 merged (PR #1), Phase 9 in progress; L1/L2 still not implemented | OUTDATED_CURRENT_STATUS |
| 10 | `docs/KNOWN_LIMITATIONS.md` P3-B2 | "`scholarships/0001_initial` (not yet merged)" | True during the Phase 3 review only | Original kept; italic later note: shipped in `d8d90f2` | HISTORICAL_RECORD |
| 11 | `docs/KNOWN_LIMITATIONS.md` D-P2 | "`docs/BUSINESS_RULES.md` does not exist … No file created." | True in Phase 4; Phase 9 created `BUSINESS_RULES.md` at the root | Original kept; italic later note | HISTORICAL_RECORD |
| 12 | `docs/KNOWN_LIMITATIONS.md` P8-B5; `docs/phase8_verification_results.txt` §5 | "the empty files were moved out of the checkout"; "No database file exists in the checkout after the work" | Not accurate: the empty `db.sqlite3-wal` / `-shm` side files stayed until Phase 9 | Correction notes added; the original sentences kept | INCORRECT_DOCUMENTATION |
| 13 | `docs/phase8_verification_results.txt` | "main and the Phase 5-7 branches are untouched; nothing merged." | Historical; merged afterwards | Original kept; labelled post-verification publication note (PR #1, `ac3759f`) appended | HISTORICAL_RECORD |
| 14 | `docs/phase6_verification_results.txt` | "Branch phase6-reconciliation exists only locally (not pushed). Nothing committed or pushed." | Historical; later published (`efca9da`) | Original kept; labelled note appended | HISTORICAL_RECORD |
| 15 | `docs/phase7_verification_results.txt` | "Nothing committed or pushed; main, phase5-withdrawals and phase6-reconciliation untouched." | Historical; later published (`87b5810`), on `main` as `f31635f` | Original kept; labelled note appended | HISTORICAL_RECORD |
| 16 | `pyproject.toml` | `requires-python = ">=3.12,<3.13"` | Contradicts the verified Python 3.14.6 support (Phases 3–8) | **Not changed** (packaging metadata, an owner decision); reported here and in §4 | INCORRECT_DOCUMENTATION |
| 17 | `README.md`, `datagen/management/commands/demo_workflow.py`, `tests/datagen/test_demo_workflow.py` (Phase 9 additions) | "~8 min, ~4.4 GB" / "About 8 minutes and 4.4 GB" for the full-scale demo | Not measured (only Phase 8 generation was); written as an estimate | Removed; the runtime and memory will be recorded when the full-scale demo is run | INCORRECT_DOCUMENTATION |

No broken links: 22 relative Markdown links and every file path cited in backticks in
README, CHANGELOG, KNOWN_LIMITATIONS, DATA_DICTIONARY and BUSINESS_RULES resolve to existing
files.

## 3. Historical statements deliberately preserved

- Every verification report's results, dates, counts, bases and "nothing committed / pushed /
  merged" sentences: they describe their own moment. Publication notes were appended, never
  substituted.
- KNOWN_LIMITATIONS decisions and observations of Phases 2–8 (including P7-O1, which names
  `demo_workflow`, DATA_DICTIONARY and API.md as Phase 9 work: still accurate).
- All commit hashes and branch names.

## 4. Frozen documents (reported, not changed)

`docs/ARCHITECTURE.md` and `docs/DATABASE_DESIGN.md` are byte-identical to `main`.

| Document | Statement | Current reality |
|---|---|---|
| ARCHITECTURE §2, §3, §9.3, §6 | `docs/API.md`, `docs/DATA_DICTIONARY.md`, `docs/BUSINESS_RULES.md` | At the repository root, at the owner's instruction (README notes the deviation) |
| ARCHITECTURE §15 R-30 | data dictionary "generated from models" | Hand-written, checked field by field against the models by `tests/architecture/test_data_dictionary.py` |
| ARCHITECTURE §16 Phase 9 | exit criterion "a fresh clone runs end to end with 3 commands" | Not yet verified (Phase 9 in progress) |
| DATABASE_DESIGN §3 Payment | `method` Char(14) | `varchar(16)` in the code (`SPONSOR_TRANSFER` has 16 characters); recorded in DATA_DICTIONARY.md |
| DATABASE_DESIGN conventions | "Every SV rule is also REC" | Resolved in Phase 6 (P6-O7; `docs/phase6_sv_coverage.md`), where stored data allows |

## 5. Remaining documentation gaps

- Phase 9 deliverables not yet written: `API.md`, `PROMPT.md`, `docs/phase9_acceptance_matrix.md`,
  `docs/phase9_verification_results.txt`, the README getting-started section, and the Phase 9
  CHANGELOG / KNOWN_LIMITATIONS entries (including two observations the demo surfaced: the
  late-fee dry run lists the injected duplicate invoices; the evaluation counts the students
  currently in the database).
- `pyproject.toml` `requires-python` (finding 16) awaits the owner's decision.
- The Phase 9 regression counts are not yet measured.

## 6. Files changed by this audit

`CHANGELOG.md`, `README.md`, `docs/KNOWN_LIMITATIONS.md`,
`docs/phase6_verification_results.txt`, `docs/phase7_verification_results.txt`,
`docs/phase8_verification_results.txt`, `datagen/management/commands/demo_workflow.py`
(docstring), `tests/datagen/test_demo_workflow.py` (docstring), and this file.
