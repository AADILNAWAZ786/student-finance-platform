# PROMPT.md — AI-assisted development record

> Historical integrity note: this document is a draft reconstructed from the project history and currently available conversation records. A section is labelled **Verbatim** only when exact source wording is available. Other phase histories are **Reconstructed prompt summary** and must not be presented as exact historical prompts.

## 1. Original YottaFlex challenge objective

Build a working student-finance backend that calculates tuition and fees, applies scholarship eligibility and payments, reconciles student balances, exposes decisions and exceptions, preserves traceability, and produces measurable evaluation outputs. The challenge also requires synthetic data with deterministic identifiers, separate ground-truth labels, validation, exports, and reproducibility.

## 2. AI-assisted development approach

Claude was used as an implementation assistant for repository inspection, design proposals, Django code, tests, data generation, anomaly evaluation, API documentation, and delivery records. The owner reviewed proposed plans, approved scope, ran local PowerShell commands, inspected reports, and controlled GitHub publication.

## 3. Phase-wise development history

### Phase 0 — foundation

**Reconstructed prompt summary:** establish the Django project foundation, configuration, core utilities, health endpoint, database routing, and initial checks. Verification was performed through the recorded Phase 0 tests.

### Phase 1 — students, programs, terms and enrollments

**Reconstructed prompt summary:** build the student and academic master-data foundation, identifiers, API validation, permissions, and tests.

### Phase 2 — fees and billing

**Reconstructed prompt summary:** implement fee schedules, invoices, invoice lines, waivers, lifecycle status, late fees, ledger entries, and balance/statement views with auditable changes.

### Phase 3 — scholarships

**Reconstructed prompt summary:** implement scholarship types, eligibility evaluation, awards, applications, constraints, and scholarship allocation history.

### Phase 4 — payments and refunds

**Reconstructed prompt summary:** implement payments from multiple payer types, automatic/manual allocation, scholarship precedence, unallocated credit, refund request/approval/process lifecycle, and ledger traceability.

### Phase 5 — withdrawals and cancellations

**Reconstructed prompt summary:** implement term withdrawals and pre-term cancellations, refund quote calculations, release of scholarship and payment coverage, append-only adjustments, and explicit refund requests.

### Phase 6 — reconciliation

**Reconstructed prompt summary:** implement balance snapshots, structural/relationship/temporal/lifecycle/arithmetic checks, five required anomaly families, per-student balance identities, and findings without mutating business data.

### Phase 7 — synthetic data and evaluation

**Reconstructed prompt summary:** build a deterministic in-memory dataset pipeline, domain-derived values, anomaly injection, separate ground-truth labels, blind detection, and precision/recall/F1 evaluation.

### Phase 8 — full-scale tooling

**Reconstructed prompt summary:** add configurable generation validation, CSV/JSON/SQL exports, manifests, export verification, two-process reproducibility checks, full-scale validation, and database-safety tests. Recorded verification reported 2,698 passed, 3 skipped and 2 expected XFAILs on Python 3.14.6 and 3.12.3, plus opt-in full-scale tests. These are historical reported results and should be checked against the attached verification report.

### Phase 9 — demo and documentation

**Reconstructed prompt summary:** add an isolated end-to-end demo, field-level data dictionary, business-rule catalogue, API documentation rendered from OpenAPI and captured demo traffic, an AI development record, a challenge acceptance matrix, and final verification. The exact status of Phase 9 deliverables must be taken from the latest local branch and verification report, not inferred from this draft.

## 4. Important prompts and engineering instructions

Repeated instructions included: inspect before editing; preserve frozen design files; use valid primary/foreign keys; calculate derived financial values from source records; preserve append-only history; keep ground-truth labels separate; avoid personal database paths; use deterministic seeds; verify actual endpoints rather than inventing documentation; retain known limitations and expected XFAILs; and do not publish or merge without owner review.

## 5. Architecture and database design instructions

The design documents `docs/ARCHITECTURE.md` and `docs/DATABASE_DESIGN.md` were treated as frozen. Changes that conflict with them were to be reported as deviations rather than silently editing the frozen documents.

## 6. Business logic implementation instructions

Business calculations were to remain in domain services/calculators, with API serializers and views delegating to them. State transitions, allocations, refunds, withdrawals, and reconciliation findings were to preserve traceability and validation.

## 7. Synthetic data generation and anomaly detection prompts

Generate valid parent/master data before dependent records; use stable deterministic identifiers and fixed seeds; derive totals and balances from source records; inject only explicitly labelled edge cases; keep ground truth in a separate store; and measure detector outcomes against those labels.

## 8. Testing, verification and debugging instructions

Use targeted tests followed by full regression; run Ruff, Django checks, migration checks and OpenAPI validation; preserve the two documented expected XFAILs; test multiple supported Python versions sequentially; and report the real command output rather than estimated results.

## 9. Reproducibility and database safety instructions

Use isolated temporary databases for generation, export, demos, and reproducibility. Probe database settings before migrations or flushes. Compare deterministic output by file hashes, excluding only documented execution metadata. Record database hashes before and after testing.

## 10. Git workflow and review process

Development was organized by phases and branches. The owner controls GitHub publication. Local commits and bundles may be prepared for review, but no `git push`, force push, pull request, or merge should occur without the owner's explicit instruction.

## 11. Human review and AI limitations

AI-generated code and tests require inspection and execution. Reported pass counts, runtime, memory, and acceptance statuses are not independent proof unless the corresponding command was run and its output preserved. Known mistakes and limitations should be disclosed, not hidden.

## 12. Known limitations

The two documented role/access design limitations remain: learner access to their own student record and instructor access to enrollments in their own programs. Any other limitation must be taken from `docs/KNOWN_LIMITATIONS.md` and the final acceptance matrix.
