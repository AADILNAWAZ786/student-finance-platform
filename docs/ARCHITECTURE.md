# Student Finance Backend — Architecture

YottaFlex AI challenge: *Calculate tuition and fees, apply scholarship eligibility and payments, and reconcile student balances.*

Status: **Design v1.1, ready to freeze.** Rule IDs (`BR-…`), requirement IDs (`R-…`), error codes, calculator contracts and endpoint paths in this document are the contract that code, tests and API docs refer to. `DATABASE_DESIGN.md` v1.1 is the companion document.

### Change log
| Version | Changes |
|---|---|
| v1.0 | Initial baseline. Later patches: SHA-256 stage seeds; eligibility count; withdrawal day boundaries; invoice statuses; full-ride scholarship. |
| **v1.1** | Full review. BR-ALC-02 now includes adjustments; release order on adjustment (scholarship, then payments); scholarship precedence over payments (BR-ALC-06); invoice **lifecycle status separated from collection status**; VOID now posts adjustments; ledger posting table + worked numerical example (§7); refund caps per payment; fee schedules fully immutable (effective_to derived); deterministic exports separated from execution metadata; SQLite limits checked against Django 5.2 (incl. Decimal-as-REAL); 2026 calendar moved so every ID carries `-2026-`; historical admit terms; exact baseline volumes vs injected rows; ground truth in a separate database the detector cannot reach; real test files; requirement traceability matrix with IDs. |

---

## 1. Goals and design principles

| # | Principle | What it means in code |
|---|---|---|
| P1 | **One source of truth for rules** | Every financial rule lives once, in a pure calculator function. API services, the synthetic data generator and the reconciliation engine all call the same function. |
| P2 | **Append-only history** | Financial records are never edited or deleted. Corrections are new rows: adjustment lines, de-allocations, reversals, status events, new fee-schedule versions. |
| P3 | **Derived, not stored-and-trusted** | Balances, open amounts and statuses are computed from source rows. Any stored derived value (invoice total, stored status, balance snapshot) exists to be *checked*. |
| P4 | **Deterministic content** | Dataset content depends only on seed + config + code. It never depends on the wall clock, unseeded randomness, hash randomization or row insertion order. Execution timestamps exist only as metadata, outside the dataset and its checksums (§11). |
| P5 | **Traceable** | Every ledger entry points to the one source row that caused it. Every rule has an ID that appears in error responses, findings and test names. |
| P6 | **Workflow APIs, not just CRUD** | Business actions are explicit POST endpoints (`/generate`, `/allocate`, `/withdraw`, `/apply`, `/runs`). Financial records can't be PATCHed or DELETEd. |
| P7 | **Atomic** | Each business action is one DB transaction. It succeeds completely or leaves no trace. |
| P8 | **Blind detection** | The anomaly detector never sees ground truth. Only the separate evaluation step compares the two (§12). |

---

## 2. Technology stack

| Concern | Choice |
|---|---|
| Language | Python 3.12 |
| Framework | **Django 5.2 LTS.** Security support runs to April 2028, and it supports Python 3.10–3.14. |
| API | Django REST Framework, django-filter |
| API docs | drf-spectacular (OpenAPI 3 schema, Swagger UI, ReDoc) + `docs/API.md` workflow guide |
| Database | SQLite, two files: `db.sqlite3` (domain data, alias `default`) and `ground_truth.sqlite3` (labels only, alias `ground_truth`, §12). Settings in §8. |
| Money | `Decimal`, 2 dp, `ROUND_HALF_UP`, currency **INR**. Floats are never used for money in Python. SQLite's own decimal handling is limited, so see §8.3. |
| Tests | pytest, pytest-django, factory_boy, coverage |
| Quality | ruff (lint + format) |

Exact versions are pinned in `requirements.txt` at Phase 0.

---

## 3. Project layout

```
student_finance/
├── manage.py
├── requirements.txt
├── pytest.ini                  # pythonpath=. ; DJANGO_SETTINGS_MODULE added in Phase 0
├── config/                     # Django project package
│   ├── settings.py             # 2 SQLite DBs, router, DRF, spectacular, money settings
│   ├── routers.py              # GroundTruthRouter (§12)
│   ├── urls.py                 # /api/v1/ … , /api/docs/, /api/schema/
│   └── generation.yaml         # seed, scale presets, distributions, quotas, anomaly rates
├── core/                       # shared, not a business domain
│   ├── models.py               # IdSequence, StatusEventBase (abstract)
│   ├── ids.py                  # deterministic ID formatting + sequence allocation
│   ├── money.py                # Decimal helpers, rounding, Python-side sums
│   ├── exceptions.py           # DomainError hierarchy → API error mapping
│   ├── api.py                  # exception handler, pagination, base viewsets
│   └── permissions.py          # role-based permissions
├── students/                   # Student, StudentStatusEvent
├── academics/                  # Program, Term, Enrollment, EnrollmentEvent
├── billing/                    # FeeSchedule, Invoice, InvoiceLine, InvoiceEvent, LedgerEntry
├── scholarships/               # ScholarshipType, ScholarshipTypeProgram, Eligibility,
│                               #   Scholarship, ScholarshipAllocation, ScholarshipEvent
├── payments/                   # Payment, PaymentAllocation, Refund, RefundEvent
├── reconciliation/             # BalanceSnapshot, ReconciliationRun, AnomalyFinding
│   └── checks/                 # one module per check family (detector; blind to labels)
├── datagen/                    # GenerationRun, GroundTruthLabel (ground_truth DB),
│   ├── rng.py                  #   stage_rng (§11)
│   ├── pipeline/               #   generator stages, anomaly injector, ID materialization
│   ├── exporters/              #   CSV / JSON / SQL / manifests
│   ├── evaluation.py           #   the ONLY code that reads both findings and labels
│   └── management/commands/    # generate_data, export_data, evaluate, verify_reproducibility, …
├── tests/
│   ├── unit/                   # calculators (no DB)
│   ├── architecture/           # import/isolation rules
│   ├── services/               # services against DB (atomicity, rules)
│   ├── api/                    # endpoint + validation tests
│   ├── workflow/               # end-to-end 11-step scenario through the API
│   └── datagen/                # starter-scale generation, validation, reproducibility
├── docs/
│   ├── ARCHITECTURE.md  DATABASE_DESIGN.md  DATA_DICTIONARY.md  API.md  BUSINESS_RULES.md
└── output/<scale>/             # generated (git-ignored except manifests), layout in §11.4
```

Each domain app has the same internal layout:

```
<app>/
├── models.py          # fields + single-row, non-arithmetic DB constraints only (§8.3)
├── calculators.py     # pure functions: inputs → Decimal/result dataclasses (no ORM)
├── lifecycle.py       # allowed status transitions (single definition)
├── services.py        # orchestration: atomic, cross-row validation, events, ledger
├── serializers.py     # request/response shapes, field-level validation
├── views.py           # thin: parse → call service → serialize
├── urls.py  filters.py  admin.py
```

---

## 4. App responsibilities and dependencies

```
            core  (ids, money, exceptions, base models)
              ▲
   students ◄─┴─► academics
       ▲             ▲
       └──── billing ┘          (FeeSchedule, Invoice, InvoiceLine, LedgerEntry)
               ▲    ▲
     scholarships   payments    (both post to billing's ledger)
               ▲    ▲
          reconciliation        (detector: reads domain DB, writes findings/snapshots)
               ▲
            datagen             (drives calculators/services; owns labels + evaluation)
```

Dependencies only point upward (no cycles). `scholarships` and `payments` never import each other. When a withdrawal touches both, `academics.services.withdraw_enrollment` orchestrates them through their public service functions. **No app except `datagen` may import from `datagen`**; `tests/architecture/test_ground_truth_isolation.py` enforces this.

| App | Owns | Key services |
|---|---|---|
| **students** | Student master data, status lifecycle | `create_student`, `change_status`, `get_balance`, `get_statement` |
| **academics** | Programs, terms, enrollments | `enroll`, `withdraw_enrollment`, `cancel_enrollment` |
| **billing** | Fee schedules (versioned), invoices, lines, ledger | `resolve_fee_schedule`, `generate_invoice`, `assess_late_fees`, `void_invoice`, `refresh_collection_status`, `post_ledger` |
| **scholarships** | Criteria, eligibility evaluations, awards, application to lines | `evaluate_eligibility`, `award_scholarship`, `apply_scholarship`, `reverse_for_adjustment` |
| **payments** | Payments, allocations, refunds | `record_payment`, `allocate_payment`, `deallocate`, `request_refund`, `approve_refund`, `process_refund` |
| **reconciliation** | Snapshots, runs, findings | `take_snapshot`, `run_reconciliation` |
| **datagen** | Generation runs, ground truth, exports, evaluation | `generate`, `inject_anomalies`, `export`, `evaluate`, `verify_reproducibility` |

---

## 5. Layering and calculator contracts

```
HTTP request
   │
   ▼
View (DRF ViewSet / APIView)        thin; permissions; no business logic
   │  serializer.is_valid()         types, enums, required, amount > 0, ≤ 2 dp, formats
   ▼
Service function (@transaction.atomic, IMMEDIATE on SQLite)
   │  load rows (select_for_update(): kept for PostgreSQL portability, a no-op on SQLite §8)
   │  cross-row validation           ← raises DomainError(code, rule_id, details)
   │  call calculator(s)             ← pure, deterministic, unit-tested
   │  write new rows (+ events + ledger entries)
   ▼
Models / DB constraints              last line of defence (FK, UNIQUE, sign/range CHECKs)
```

**Calculator contracts** are pure functions on plain objects or dataclasses, with no ORM access. The unit tests in `tests/unit/` are written against these exact signatures:

| Function | Purpose | Rules |
|---|---|---|
| `billing.calculators.resolve_fee_version(versions, on_date)` | pick the version in force | BR-FEE-01/03 |
| `billing.calculators.effective_windows(versions)` | derive each version's `effective_to` | BR-FEE-03 |
| `billing.calculators.price_lines(enrollment, fee_rows)` | priced FEE lines | BR-FEE-02/04, BR-WAV-01 |
| `billing.calculators.late_fee(open_amount, due_date, as_of, policy)` | late fee or 0 | BR-LATE-01 |
| `billing.calculators.invoice_amounts(lines, scholarship_allocations, payment_allocations)` → `InvoiceAmounts` | separated amounts | BR-INV-04 |
| `billing.calculators.collection_status(amounts, *, due_date, as_of)` | collection status | BR-INV-06 |
| `billing.lifecycle.lifecycle_transition_allowed(current, target)`, `billing.lifecycle.can_void(amounts)` | lifecycle rules | BR-INV-08 |
| `billing.calculators.release_for_adjustment(*, line_net, adjustment, scholarship_allocated, payment_allocations)` → `ReleasePlan` | what to reverse and deallocate | BR-WDR-03 |
| `billing.calculators.LEDGER_SIGN` | sign per entry type | BR-LED-01 |
| `scholarships.calculators.evaluate(student, term, scholarship_type)` → `EligibilityResult` | eligibility + reason codes | BR-SCH-01 |
| `scholarships.calculators.coverable_lines(type, lines)`, `eligible_fee_base(type, lines)`, `award_amount(type, eligible_base, already_awarded)` | coverage and amount | BR-SCH-03/04/06/08 |
| `payments.calculators.plan_allocation(amount, open_lines)` → `list[AllocCalc]` | AUTO allocation | BR-ALC-01..04 |
| `payments.calculators.max_refundable(net_paid, credit_balance, payment_available)` | refund cap | BR-REF-01/02 |
| `academics.calculators.withdrawal_refund_pct(term, withdrawal_date)` | schedule % | BR-WDR-02 |
| `reconciliation.calculators.balance_from_ledger(entries, as_of=None)` | ledger balance | BR-BAL-01 |
| `datagen.rng.stage_rng(seed, stage_name)` | deterministic sub-stream | §11 |

The **generator** calls the calculators directly and writes with `bulk_create`. The **API** calls services, and services call the same calculators. `tests/datagen/test_parity.py` replays a sample of generated records through the services and asserts identical results.

---

## 6. Business rules (summary; full catalogue in `BUSINESS_RULES.md`)

### 6.1 Fees and invoicing
| ID | Rule |
|---|---|
| BR-FEE-01 | Each enrollment is priced from the FeeSchedule versions for its **program + term + residency** (or `ALL`) that are **in force on the term's `invoice_date`**. If no version is in force → `422 FEE_SCHEDULE_NOT_FOUND`. |
| BR-FEE-02 | `PER_CREDIT` lines: quantity = enrolled credits. `FLAT` lines: quantity = 1. `gross = quantity × unit_amount`. |
| BR-FEE-03 | **Fee schedule rows are fully immutable.** A price change inserts a new version row (`version + 1`, `supersedes` → previous, its own `effective_from`). Nothing on the previous row changes. `effective_to` is not stored: each version is in force from its `effective_from` until the day before the next version's `effective_from` (open-ended for the latest). A new version's `effective_from` must be later than its predecessor's, and the chain is linear (`supersedes` is unique), so windows can't overlap or have gaps. The version history is the set of rows. |
| BR-FEE-04 | `LAB` lines are only charged when `program.has_lab`. `INTERNATIONAL_SERVICE` only for international students. |
| BR-WAV-01 | A waiver reduces one line: `0 ≤ waiver_amount ≤ gross`, `waiver_reason` required when > 0, and only on `is_waivable` fee types. `net = gross − waiver` (may be 0 for a fully waived line). |
| BR-INV-01 | At most **one invoice with `lifecycle_status ≠ VOID` per enrollment**. A second generation attempt → `409 DUPLICATE_INVOICE`. |
| BR-INV-02 | `invoice.total_amount = Σ net of its FEE lines` at issue. It's immutable; later changes are separate lines. |
| BR-INV-03 | `issue_date = term.invoice_date` (or `enrolled_on` if later); `due_date = term.due_date`; `issue_date ≤ due_date`. |
| BR-INV-04 | Invoice amounts (computed by `invoice_amounts`, never stored) keep **three kinds of reduction separate**: **original_charges** = Σ net of FEE + LATE_FEE lines (after waivers). **adjustments** = Σ ADJUSTMENT lines (≤ 0; withdrawal, cancellation or void credits). They reduce what is owed and are **never payments and never scholarship**. **net_charges** = original_charges + adjustments. **scholarship_applied** = Σ ScholarshipAllocation (APPLY + REVERSE, signed). **amount_paid** = Σ PaymentAllocation (ALLOCATE + DEALLOCATE, signed), from **every payer**, broken down in `payments_by_payer` (SELF / GUARDIAN / SPONSOR). **outstanding** = net_charges − scholarship_applied − amount_paid. Negative scholarship_applied, amount_paid or outstanding is an arithmetic error: the calculator raises, and reconciliation reports `ARITHMETIC_MISMATCH`. |
| BR-INV-05 | **Paid vs settled.** *Paid* = money received through Payment records went to the invoice (`amount_paid`, any payer, BR-PAY-03). *Settled* = `outstanding = 0` (`is_settled`), whether by payments, by scholarship, or because adjustments left nothing to pay. Every PAID invoice is settled, but a settled invoice isn't necessarily PAID. Scholarship credit and adjustments are never reported as money paid on invoices, statements or balances, and they have their own ledger entry types. |
| BR-INV-06 | **Collection status** (`collection_status(amounts, due_date, as_of)`), first match wins. **outstanding > 0:** `as_of > due_date` → **`OVERDUE`**; else amount_paid > 0 → **`PARTIALLY_PAID`**; else **`UNPAID`**. **outstanding = 0:** net_charges = 0 → **`ADJUSTED_TO_ZERO`**; else amount_paid > 0 → **`PAID`**; else **`SCHOLARSHIP_COVERED`** (then scholarship_applied = net_charges > 0). So a zero balance is SCHOLARSHIP_COVERED only when something is still charged after adjustments, no money from any payer was applied, and scholarship covers all of it. Partial scholarship with no payment is `UNPAID`. Overdue starts the day after `due_date` (on the due date itself it isn't overdue). The late fee keeps its own grace (BR-LATE-01). |
| BR-INV-07 | Collection status is recalculated in the same transaction as every invoice issue, scholarship apply/reverse, payment allocate/deallocate, adjustment and late-fee line. `refresh_collection_status(as_of)` moves past-due invoices to OVERDUE. Each change writes an InvoiceEvent (`dimension = COLLECTION`) and sets `collection_status_as_of`. Reconciliation recomputes BR-INV-06 at each invoice's `collection_status_as_of` and compares (`INVOICE_STATUS_MISMATCH`). |
| BR-INV-08 | **Lifecycle status** is separate: `ISSUED → CANCELLED` (enrollment cancelled, BR-WDR-01) and `ISSUED → VOID` (`void_invoice`, allowed only when scholarship_applied = 0 and amount_paid = 0). `CANCELLED` and `VOID` are terminal. Voiding posts an ADJUSTMENT line (`reason VOID`) of −net for every line with net > 0, so a void invoice's collection status is `ADJUSTED_TO_ZERO` and its charges leave the ledger without deleting anything. Lifecycle changes write InvoiceEvent (`dimension = LIFECYCLE`). A cancelled invoice keeps a live collection status, so a still-owed registration fee shows as UNPAID/OVERDUE. |
| BR-LATE-01 | Late fee: if an invoice with lifecycle `ISSUED` still has outstanding > 0 at the end of `due_date + grace_days (14)`, a `LATE_FEE` line is posted dated the next day. Amount = **2% of outstanding, minimum ₹500, maximum ₹5,000**. Once per invoice. Late fees are never scholarship-eligible, waivable or refundable. |

### 6.2 Scholarships
| ID | Rule |
|---|---|
| BR-SCH-01 | Eligibility is evaluated per student × scholarship type × term against the type's criteria (GPA, need index, program, residency, first-term). Each evaluation is stored with its input snapshot and reason codes. Eligible or not, every evaluation is kept. |
| BR-SCH-02 | An award requires a stored, positive Eligibility row for the same student, type and term (and at most one award per Eligibility row). |
| BR-SCH-03 | Award amount = `PERCENT × eligible base` or `FIXED`, capped by the type's `max_amount_per_term`. |
| BR-SCH-04 | **Σ awards applied to an invoice ≤ Σ net of its scholarship-eligible lines.** An over-cap request → `422 SCHOLARSHIP_EXCEEDS_ELIGIBLE_FEES`. (Full-ride: see BR-SCH-08.) |
| BR-SCH-05 | At most one award per `exclusive_group` per term (e.g. one MERIT award). |
| BR-SCH-06 | Scholarships apply only to `is_scholarship_eligible` lines, in allocation-priority order. (Full-ride: see BR-SCH-08.) |
| BR-SCH-07 | On withdrawal, applied scholarship is reversed by the same percentage as the tuition adjustment, so BR-SCH-04 still holds afterwards. This is applied per adjusted line; mechanics in BR-WDR-03. |
| BR-SCH-08 | **Full-ride scholarship** (`SCT-007`, `coverage_scope = ALL_FEE_LINES`). It's the **only** type that covers **all FEE lines**, including REGISTRATION, TECHNOLOGY, LIBRARY, STUDENT_ACTIVITY and INTERNATIONAL_SERVICE. It never covers LATE_FEE or ADJUSTMENT lines. For this type, the eligible base in BR-SCH-03/04 is Σ net of all FEE lines, and lines are covered in allocation-priority order. A full-ride award excludes every other award for the same student and term: it's evaluated and awarded first (priority 0), and combining it with any other award → `409 SCHOLARSHIP_GROUP_CONFLICT`. Eligibility for the other types is still evaluated and stored. SCT-001..006 keep `coverage_scope = ELIGIBLE_LINES_ONLY`, so BR-SCH-04/06 are unchanged for them. On withdrawal, only full-ride allocations on adjusted lines are reversed (BR-SCH-07). Non-refundable lines stay covered, so no payment is needed and no refund is due. |

### 6.3 Payments and allocation
| ID | Rule |
|---|---|
| BR-PAY-01 | Payment amount > 0. `external_reference` is unique, so resubmitting returns `409 DUPLICATE_PAYMENT`. |
| BR-PAY-02 | A payment is credited to the student's ledger in full when received. Allocation only distributes it across lines and posts nothing to the ledger. |
| BR-PAY-03 | Every Payment counts as money paid, whatever its `payer_type` (`SELF`, `GUARDIAN`, `SPONSOR`) or `method`. Sponsor and guardian money is recorded as Payments, never as scholarships, and counts toward `amount_paid`, PAID / PARTIALLY_PAID status and net paid for refunds. |
| BR-ALC-01 | **Σ net allocations of a payment ≤ payment amount − Σ its refunds not REJECTED.** Breaking it → `422 ALLOCATION_EXCEEDS_PAYMENT`. |
| BR-ALC-02 | **Line open amount** = line net **+ Σ ADJUSTMENT lines that adjust it (withdrawal, cancellation, void)** − Σ net scholarship allocations on it − Σ net payment allocations on it. Allocations (scholarship or payment) go only to FEE and LATE_FEE lines, and never beyond the open amount (`422 ALLOCATION_EXCEEDS_LINE_OPEN`). Invariant: open ≥ 0 on every line at all times. |
| BR-ALC-03 | AUTO allocation order: oldest invoice `due_date` first, then line `allocation_priority` (tuition first, late fee last), then `line_id`. Lines with open = 0 are skipped. |
| BR-ALC-04 | Any remainder after allocation stays as an **unallocated credit** (overpayment). It can be allocated later or refunded. |
| BR-ALC-05 | Allocations are never edited. Freeing money creates a `DEALLOCATE` row (negative) that references the original `ALLOCATE` row, with Σ deallocations ≤ the original. |
| BR-ALC-06 | **Scholarship precedence.** Scholarship coverage is decided by the scholarship rules; payments only ever cover the remainder. When a scholarship is applied to a line already covered by payments, payment allocations on that line are deallocated last-in-first-out (`rule_applied = SCHOLARSHIP_PRECEDENCE`) to make room. The freed money becomes unallocated credit. |

### 6.4 Withdrawals, cancellations and refunds
| ID | Rule |
|---|---|
| BR-WDR-01 | **Cancellation** = before `term.start_date`. Every line except `REGISTRATION` (non-refundable) gets an ADJUSTMENT of −net (reason `CANCELLATION`). Enrollment → CANCELLED; invoice lifecycle → CANCELLED (BR-INV-08). The invoice's collection status stays live on whatever is still owed. |
| BR-WDR-02 | **Withdrawal** = on or after start. Day number `d = (withdrawal_date − term.start_date).days`, so day 0 = start date. Refundable lines (TUITION, LAB) get a credit adjustment of: **d 0–7 → 80%**, **d 8–14 → 60%**, **d 15–27 → 40%**, **d ≥ 28 → 0%**. Day 28 is the census date (`term.census_date = start_date + 28 days`). Withdrawing on the census date gives no refund. All ranges are inclusive and don't overlap. Adjustment = −round_half_up(line net × pct). |
| BR-WDR-03 | **Release order when an adjustment is posted on a line** (`release_for_adjustment`). Let `N` = line net before the adjustment, `A` = adjustment (≤ 0), `N' = N + A`, `S` = net scholarship on the line, `P` = net payment allocations. **Step 1, scholarship first:** reverse `R = min(S, max(round_half_up(S × |A| / N), S − N'))`. That's the proportional share (BR-SCH-07). The `S − N'` term is a guard: under ROUND_HALF_UP it never binds, because S × |A| / N ≥ S − N' and rounding can't fall below a value already on the 2-dp grid. A sweep test checks this. **Step 2, then payments:** `excess = (S − R) + P − N'`. If excess > 0, deallocate that much from the line's payment allocations, last-in-first-out by (`allocated_on`, `allocation_id`) descending. Freed payment money becomes unallocated credit and can be refunded (BR-REF). A zero adjustment releases nothing. |
| BR-REF-01 | **Refund ≤ net paid** (Σ payments − Σ refunds not REJECTED) → otherwise `422 REFUND_EXCEEDS_NET_PAID`. |
| BR-REF-02 | Refund ≤ the student's **credit balance** (−ledger balance, if the balance is negative) → otherwise `422 REFUND_EXCEEDS_CREDIT_BALANCE`. It must also be ≤ the source payment's **available amount** (payment amount − Σ its net allocations − Σ its refunds not REJECTED) → otherwise `422 REFUND_EXCEEDS_PAYMENT_AVAILABLE`. A REQUESTED or APPROVED refund reserves its amount, so two pending requests can't spend the same money. |
| BR-REF-03 | Lifecycle: `REQUESTED → APPROVED → PROCESSED`, or `REQUESTED/APPROVED → REJECTED`. The ledger is debited only on PROCESSED. |
| BR-REF-04 | A refund goes back to the payer of its source payment (`payer_type` copied onto the refund), so sponsor money returns to the sponsor. |

### 6.5 Ledger, balances and reconciliation
| ID | Rule |
|---|---|
| BR-LED-01 | **Posting table.** Each source row posts **exactly one** ledger entry, and allocations of payments post none. Debit (+) = the student owes more; credit (−) = the student owes less. |
| BR-BAL-01 | `ledger balance(as_of) = Σ LedgerEntry.amount with entry_date ≤ as_of`. Negative = credit balance. |
| BR-BAL-02 | **Subledger identity**, per student: `ledger balance = Σ invoice outstanding − unallocated credit`, where `unallocated credit = Σ payments − Σ net payment allocations − Σ PROCESSED refunds`. (Proof: substitute BR-LED-01 into both sides.) |
| BR-BAL-03 | `BalanceSnapshot.reported_balance` must equal BR-BAL-01 at its `as_of_date`. A mismatch is a `BALANCE_LEDGER_MISMATCH` finding. |

BR-LED-01 posting table:

| Source row | entry_type | Sign | Ledger amount |
|---|---|---|---|
| InvoiceLine `FEE` | `CHARGE` | + (≥ 0; 0 for a fully waived line) | +net |
| InvoiceLine `LATE_FEE` | `LATE_FEE` | + | +net |
| InvoiceLine `ADJUSTMENT` (withdrawal / cancellation / void) | `ADJUSTMENT` | − | net (already negative) |
| ScholarshipAllocation `APPLY` | `SCHOLARSHIP_CREDIT` | − | −amount |
| ScholarshipAllocation `REVERSE` (amount < 0) | `SCHOLARSHIP_REVERSAL` | + | −amount |
| Payment | `PAYMENT` | − | −amount |
| PaymentAllocation `ALLOCATE` / `DEALLOCATE` | *(none)* | | |
| Refund on `PROCESSED` | `REFUND` | + | +amount |

Zero-amount adjustment or allocation rows are never created.

---

## 7. Worked example: invoice, unallocated credit and ledger reconcile

Student on TERM-2026-FALL (start Mon 07 Sep, due 14 Sep), domestic, lab program, 16 credits at ₹10,000. IDs are illustrative. `tests/unit/test_worked_example.py` checks every number below.

**Invoice (issued 17 Aug), FEE lines:**

| Line | Fee type | Gross | Waiver | Net | Eligible (std) | Refundable |
|---|---|---|---|---|---|---|
| L01 | TUITION 16 × 10,000 | 160,000 | 0 | 160,000 | ✓ | ✓ |
| L02 | LAB | 12,000 | 0 | 12,000 | ✓ | ✓ |
| L03 | REGISTRATION | 5,000 | 0 | 5,000 | | |
| L04 | TECHNOLOGY | 4,000 | 0 | 4,000 | | |
| L05 | LIBRARY | 2,000 | 0 | 2,000 | | |
| L06 | STUDENT_ACTIVITY | 3,000 | 3,000 (HARDSHIP) | **0** | | |
| | **total_amount** | | | **183,000** | | |

**Stage A: scholarship and payments**
1. SCT-001 Merit 50% of eligible base 172,000 = **86,000** (cap 1,00,000). Applied to L01 (17 Aug).
2. PAY-1 from **GUARDIAN** ₹60,000 (25 Aug), AUTO → L01 60,000. Status: outstanding 37,000 → `PARTIALLY_PAID`.
3. PAY-2 from **SPONSOR** ₹50,000 (10 Sep), AUTO → L01 14,000, L02 12,000, L03 5,000, L04 4,000, L05 2,000 (L06 skipped, open 0). **13,000 unallocated** (overpayment).

| | Invoice | Student |
|---|---|---|
| net_charges | 183,000 | |
| scholarship_applied | 86,000 | |
| amount_paid (guardian 60,000 + sponsor 37,000) | 97,000 | |
| outstanding → status | **0 → `PAID`** | |
| unallocated credit = 110,000 − 97,000 − 0 | | 13,000 |
| ledger = 183,000 − 86,000 − 60,000 − 50,000 | | **−13,000** |
| identity: Σ outstanding − unallocated = 0 − 13,000 | | **−13,000 ✓** |

**Stage B: withdrawal on 17 Sep (day 10 → 60%)**

| Line | N | A | N' | S | R (step 1) | P | Excess (step 2) | Deallocations (LIFO) |
|---|---|---|---|---|---|---|---|---|
| L01 | 160,000 | −96,000 | 64,000 | 86,000 | **51,600** | 74,000 | 34,400 + 74,000 − 64,000 = **44,400** | PAY-2 L01 14,000, then PAY-1 L01 30,400 |
| L02 | 12,000 | −7,200 | 4,800 | 0 | 0 | 12,000 | **7,200** | PAY-2 L02 7,200 |

After release:

| | Invoice | Student |
|---|---|---|
| original_charges / adjustments / net_charges | 183,000 / −103,200 / 79,800 | |
| scholarship_applied = 86,000 − 51,600 | 34,400 | |
| amount_paid = 29,600 + 4,800 + 5,000 + 4,000 + 2,000 | 45,400 | |
| outstanding → status | **0 → `PAID`** (lifecycle stays `ISSUED`) | |
| unallocated credit = 110,000 − 45,400 − 0 | | 64,600 (PAY-1 30,400 + PAY-2 34,200) |
| ledger = −13,000 − 96,000 − 7,200 + 51,600 | | **−64,600** |
| identity: 0 − 64,600 | | **−64,600 ✓** |

**Stage C: refunds (requested 18 Sep, processed 30 Sep)**
- `max_refundable` = min(net paid 110,000, credit balance 64,600, per-payment available) = 64,600 in total.
- REF-1 ₹34,200 from PAY-2 → back to the sponsor. REF-2 ₹30,400 from PAY-1 → back to the guardian.
- A request of ₹35,000 against PAY-1 → `422 REFUND_EXCEEDS_PAYMENT_AVAILABLE` (available 30,400).
- After processing: ledger −64,600 + 64,600 = **0**; unallocated = 110,000 − 45,400 − 64,600 = **0**; Σ outstanding = 0 → identity **0 = 0 − 0 ✓**. Net paid = 45,400 = amount_paid ✓.

Ledger for this invoice: 6 CHARGE (incl. one of ₹0), 1 SCHOLARSHIP_CREDIT, 2 PAYMENT, 2 ADJUSTMENT, 1 SCHOLARSHIP_REVERSAL, 2 REFUND = 14 entries. Payment allocations and deallocations post none.

---

## 8. Transactions, locking and SQLite

### 8.1 Settings (checked against the Django 5.2 docs)

```python
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
        "OPTIONS": {
            "transaction_mode": "IMMEDIATE",  # Django ≥ 5.1
            "timeout": 20,  # seconds to wait for the write lock
            "init_command": "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; PRAGMA foreign_keys=ON;",
        },
    },
    "ground_truth": {  # labels only (§12)
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "ground_truth.sqlite3",
        "OPTIONS": {"transaction_mode": "IMMEDIATE", "timeout": 20},
    },
}
DATABASE_ROUTERS = ["config.routers.GroundTruthRouter"]
```

### 8.2 Locking limitations and the strategy

| Fact (Django 5.2 / SQLite) | Consequence for this design |
|---|---|
| `select_for_update()` has no effect on SQLite. | Services still call it so the code is correct on PostgreSQL. On SQLite, correctness comes from the database-level write lock below, not from row locks. |
| SQLite allows **one writer at a time for the whole database**. `transaction_mode: "IMMEDIATE"` takes the write lock at `BEGIN`, and the docs recommend it so transactions wait up to `timeout` instead of failing with "database is locked". | Every write service runs as one `transaction.atomic()` block. Because the lock is taken at the start, each service's validate-then-write is serialized: two allocations against the same payment can't interleave. |
| IMMEDIATE applies to every atomic block, including read-only ones. | Atomic blocks are kept short. Long read-only work (statements, reconciliation checks) runs **outside** atomic, and reconciliation writes its findings in one short transaction at the end. |
| WAL journal mode lets readers continue while one writer commits. | GET endpoints aren't blocked by writes. |
| Nested `atomic()` uses savepoints. | Services can call services; the outermost block owns the transaction. |
| `init_command` (Django ≥ 5.1) runs PRAGMAs on each connection. | Used for WAL, synchronous and foreign_keys. |
| `JSONField` needs SQLite's JSON1 extension (on by default from SQLite 3.38). Django 5.2 supports SQLite ≥ 3.31. | Phase 0 checks `sqlite3.sqlite_version ≥ 3.38` and fails fast otherwise. |
| Django 5.1+ uses `CheckConstraint(condition=…)`; `check=` is deprecated. | Models use `condition=`. |

Throughput is limited to one writer at a time. That's acceptable for this challenge, and the generator writes in large `bulk_create` batches (`batch_size=2000`) inside a few long transactions. `tests/services/test_concurrency.py` (Phase 4) runs two threads allocating the same payment against a file-based test DB (pytest-django `transaction=True`) and asserts BR-ALC-01 holds.

### 8.3 SQLite stores decimals as floating point

Django documents that SQLite has no real decimal type: Decimal values are stored as `REAL` (8-byte IEEE float), so SQL-side arithmetic isn't correctly rounded. Rules that follow:
1. Values round-trip correctly, because Django quantizes to `decimal_places` on read and every amount here has ≤ 12 significant digits.
2. **No money decision is made by SQL arithmetic.** Sums, comparisons and identities are computed in Python `Decimal` over fetched values (`core.money.dsum`). Any SQL `Sum()` used for display is quantized to 0.01.
3. **DB CHECK constraints are limited to signs and ranges** (`amount > 0`, `waiver_amount ≥ 0`, `0 ≤ gpa ≤ 4`). Arithmetic equalities (`net = gross − waiver`, `gross = qty × unit`) are enforced by calculators and services and verified by reconciliation in `Decimal`. A float CHECK could wrongly reject valid rows.

### 8.4 Service transaction map

| Service | Writes in one transaction |
|---|---|
| `generate_invoice` | invoice, lines, CHARGE entries, InvoiceEvents (LIFECYCLE `ISSUED`, COLLECTION `UNPAID`), id sequences |
| `assess_late_fees` | per invoice: LATE_FEE line, ledger entry, InvoiceEvent |
| `award_scholarship` | Scholarship, ScholarshipEvent |
| `apply_scholarship` | ScholarshipAllocations, SCHOLARSHIP_CREDIT entries, any BR-ALC-06 deallocations, status events |
| `record_payment` | Payment, PAYMENT entry (optionally followed by AUTO allocation in the same transaction) |
| `allocate_payment` | PaymentAllocations, InvoiceEvents |
| `withdraw_enrollment` / `cancel_enrollment` | ADJUSTMENT lines + entries, scholarship REVERSE allocations + entries, DEALLOCATE rows, Enrollment/Invoice/Scholarship events |
| `void_invoice` | ADJUSTMENT(VOID) lines + entries, InvoiceEvents |
| `request / approve / reject / process_refund` | Refund, RefundEvent, (PROCESSED) REFUND entry |
| `run_reconciliation` | ReconciliationRun + AnomalyFindings (checks run outside the transaction) |
| `core.ids.next_id` | IdSequence increment, inside the caller's transaction |

Atomicity tests force a failure partway through (e.g. patch the ledger post to raise) and assert that zero rows were written.

**Idempotency:** invoice generation is keyed on the enrollment (BR-INV-01). Payments are keyed on `external_reference` (BR-PAY-01). Reconciliation runs never change domain data.

---

## 9. API design

### 9.1 Conventions
- Base path `/api/v1/`. JSON only. Business IDs (`STU-000001`) appear in URLs and payloads.
- Money is sent as **strings** (`"12500.00"`). More than 2 decimal places → 400.
- Pagination `?page=&page_size=` (default 50, max 500). Filtering via django-filter, e.g. `?student=STU-000001&term=TERM-2026-FALL&collection_status=OVERDUE&lifecycle_status=ISSUED`.
- Financial resources are **read + action only**. `PUT/PATCH/DELETE` on fee schedules, invoices, lines, payments, allocations, refunds and ledger return 405.
- Error envelope (every 4xx):

```json
{
  "error": {
    "code": "ALLOCATION_EXCEEDS_PAYMENT",
    "rule_id": "BR-ALC-01",
    "message": "Allocations (15000.00) would exceed payment available amount (12000.00).",
    "details": {"payment_id": "PAY-2026-000042", "payment_amount": "12000.00",
                "already_allocated": "9000.00", "requested": "6000.00"}
  }
}
```

| HTTP | Used for |
|---|---|
| 400 | Malformed input, wrong types, bad enums, > 2 dp (serializer) |
| 404 | Unknown ID |
| 409 | State conflicts: duplicate invoice/payment, illegal lifecycle transition, scholarship group conflict |
| 422 | Valid shape but breaks a financial rule (cap, over-allocation, over-refund, missing fee schedule) |

### 9.2 Endpoint catalogue

**Students**
| Method | Path | Purpose |
|---|---|---|
| GET, POST | `/students/` | list (filters: program, status, residency) / create |
| GET, PATCH | `/students/{id}/` | detail / update contact fields only |
| POST | `/students/{id}/status/` | lifecycle transition (writes StudentStatusEvent) |
| GET | `/students/{id}/balance/?as_of=` | ledger balance + breakdown: charges, adjustments, scholarship credits, payments by payer, refunds, unallocated credit, BR-BAL-02 identity check |
| GET | `/students/{id}/ledger/` | ledger entries with source references |
| GET | `/students/{id}/statement/` | invoices → lines → scholarship/payment allocations, refunds |
| GET | `/students/{id}/refundable/` | max refundable amount, per-payment available amounts, calculation |

**Academics**
| Method | Path | Purpose |
|---|---|---|
| GET, POST | `/programs/`, `/terms/` | master data |
| GET, POST | `/enrollments/` | list / enroll (validates program, credits range, one per term, billable term) |
| POST | `/enrollments/{id}/withdraw/` | BR-WDR-02/03: adjustments, reversals, deallocations; returns the release plan and a refund quote |
| POST | `/enrollments/{id}/cancel/` | BR-WDR-01 |
| GET | `/enrollments/{id}/events/` | lifecycle history |

**Billing**
| Method | Path | Purpose |
|---|---|---|
| GET, POST | `/fee-schedules/` | list (program, term, fee_type, `in_force_on`) / create V1 |
| POST | `/fee-schedules/{id}/new-version/` | insert the next version (BR-FEE-03); the old row is untouched |
| GET | `/fee-schedules/{id}/history/` | the full version chain with derived windows |
| POST | `/invoices/generate/` | `{enrollment_id, waivers?: [...]}` → 201, or 409 DUPLICATE_INVOICE |
| GET | `/invoices/`, `/invoices/{id}/` | lines, line open amounts, `original_charges`, `adjustments`, `net_charges`, `scholarship_applied`, `amount_paid`, `payments_by_payer`, `outstanding`, `is_settled`, `lifecycle_status`, `collection_status`, `collection_status_as_of` |
| POST | `/invoices/{id}/void/` | BR-INV-08 (409 `INVOICE_HAS_ALLOCATIONS` otherwise) |
| POST | `/billing/late-fees/assess/` | `{as_of_date, dry_run}` batch assessment |
| POST | `/billing/collection-status/refresh/` | `{as_of_date}` |

**Scholarships**
| Method | Path | Purpose |
|---|---|---|
| GET, POST | `/scholarship-types/` | criteria definitions (immutable once referenced) |
| POST | `/scholarships/eligibility-checks/` | `{student_id, term_id}` → evaluates every active type, stores Eligibility rows, returns reasons |
| GET | `/eligibility/` | evaluation history |
| POST | `/scholarships/` | `{eligibility_id, invoice_id, requested_amount?}` → award (amount computed and capped, or 422/409) |
| POST | `/scholarships/{id}/apply/` | allocate to coverable lines (BR-SCH-06/08, BR-ALC-06) |
| GET | `/scholarships/`, `/scholarships/{id}/` | with allocations and events |

**Payments and refunds**
| Method | Path | Purpose |
|---|---|---|
| GET, POST | `/payments/` | record (409 on duplicate external_reference); `auto_allocate` flag |
| POST | `/payments/{id}/allocate/` | `{mode: "AUTO"}` or `{mode: "MANUAL", lines: [{invoice_line_id, amount}]}` |
| GET | `/payments/{id}/allocations/`, `/payment-allocations/` | allocation detail, including deallocations |
| POST | `/refunds/` | request (422 for the BR-REF-01/02 codes) |
| POST | `/refunds/{id}/approve/` · `/process/` · `/reject/` | lifecycle |

**Reconciliation (detector)**
| Method | Path | Purpose |
|---|---|---|
| POST | `/balance-snapshots/` | take a snapshot for a student / all at `as_of_date` |
| POST | `/reconciliation/runs/` | `{scope: "ALL" \| "STUDENT", student_id?, as_of_date}` → run checks |
| GET | `/reconciliation/runs/`, `/reconciliation/runs/{id}/` | summary metrics per check |
| GET | `/anomalies/` | detected findings (filters: run, issue_type, student, severity) |

**Data generation and evaluation (admin only)**
| Method | Path | Purpose |
|---|---|---|
| POST | `/datagen/runs/` | starter-scale generation (synchronous; full scale via management command) |
| GET | `/datagen/runs/`, `/datagen/runs/{id}/` | seed, scale, row counts, checksums |
| GET | `/datagen/runs/{id}/evaluation/?reconciliation_run=` | precision / recall / F1 of a reconciliation run vs ground truth (§12) |

**Docs:** `/api/schema/` (OpenAPI), `/api/docs/` (Swagger UI), `/api/redoc/`, `GET /api/v1/health/`.

### 9.3 The demonstration workflow (all through the API)

| Step | Call | Expected outcome |
|---|---|---|
| 1 | `POST /students/` | `STU-010001` created |
| 2 | `POST /enrollments/` (PRG-004, TERM-2026-FALL, 16 credits) | `ENR-2026-030001` ENROLLED |
| 3 | `POST /invoices/generate/` | `INV-STU-010001-2026-000001`, lines, CHARGE entries, `ISSUED` / `UNPAID` |
| 3b | same call again | **409 DUPLICATE_INVOICE** |
| 4 | `POST /scholarships/eligibility-checks/` | 7 results with reason codes |
| 5 | `POST /scholarships/` + `/apply/` | award capped at eligible fees; over-cap request → **422** |
| 6 | `POST /payments/` (guardian, partial) | `PAY-2026-050001`, ledger credit |
| 7 | `POST /payments/{id}/allocate/` AUTO | `ALLOC-PAY-2026-050001-01…`, tuition first → `PARTIALLY_PAID`; over-allocation → **422** |
| 8 | `GET /students/{id}/balance/` | balance = ledger sum; BR-BAL-02 identity true |
| 9 | `POST /payments/` (sponsor overpay) → `GET /refundable/` → `POST /refunds/` → approve → process | valid refund to the sponsor; over-refund → **422** |
| 10 | `POST /reconciliation/runs/` | 0 findings for this clean student |
| 11 | `GET /anomalies/?run=` | on the generated dataset: the 5 injected anomaly types detected |

The sequence mirrors §7. It's (a) documented with real request/response bodies in `docs/API.md` (captured from the running app in Phase 9, not hand-written), (b) automated in `tests/workflow/test_end_to_end.py`, and (c) runnable as `python manage.py demo_workflow`.

---

## 10. Validation strategy (preventing invalid financial transactions)

| Layer | Catches | Examples |
|---|---|---|
| Serializer | shape | missing fields, amount ≤ 0, > 2 dp, unknown enums, bad ID format, dates out of range |
| Service | business rules across rows | BR-FEE-01/03, BR-INV-01/08, BR-SCH-02/04/05/08, BR-ALC-01/02/06, BR-REF-01/02/03, illegal lifecycle transitions, credits outside program range, enrollment in a non-billable term, allocation to another student's line |
| DB constraint | single-row invariants (§8.3) | FK integrity, unique business keys, `amount > 0`, `waiver ≥ 0`, sign by type, ranges |

Domain error codes: `DUPLICATE_INVOICE`, `DUPLICATE_PAYMENT`, `FEE_SCHEDULE_NOT_FOUND`, `FEE_SCHEDULE_VERSION_INVALID`, `CREDITS_OUT_OF_RANGE`, `ENROLLMENT_EXISTS`, `TERM_NOT_BILLABLE`, `INVOICE_HAS_ALLOCATIONS`, `SCHOLARSHIP_NOT_ELIGIBLE`, `SCHOLARSHIP_EXCEEDS_ELIGIBLE_FEES`, `SCHOLARSHIP_GROUP_CONFLICT`, `ALLOCATION_EXCEEDS_PAYMENT`, `ALLOCATION_EXCEEDS_LINE_OPEN`, `ALLOCATION_STUDENT_MISMATCH`, `REFUND_EXCEEDS_NET_PAID`, `REFUND_EXCEEDS_CREDIT_BALANCE`, `REFUND_EXCEEDS_PAYMENT_AVAILABLE`, `ILLEGAL_TRANSITION`.

### Why the anomalies can still exist in the data
The API makes the four transactional anomalies impossible to create, and a balance can't disagree with the ledger because balances are derived. The dataset still *must* contain them so detection can be evaluated. They represent **legacy/imported corruption**:

1. Cross-row rules are enforced in **services, not DB constraints**. For example, there's deliberately no DB unique constraint on `invoice.enrollment`, so a duplicate invoice is *representable*.
2. The **anomaly injector** (`datagen`) writes through the ORM directly, bypassing services. It records each injected record in `GroundTruthLabel`, in the separate ground-truth database.
3. API tests prove the same anomaly is **rejected** when attempted through the endpoint.
4. The **reconciliation engine** must find every injected anomaly without seeing the labels (§12).

---

## 11. Synthetic data generation and reproducibility

### 11.1 Commands
All accept `--seed` (default `20261022`) and `--scale {starter,full}` or `--scale-factor 0.05`:

| Command | Does |
|---|---|
| `generate_data` | full pipeline into fresh `db.sqlite3` + `ground_truth.sqlite3` |
| `run_reconciliation` | the detector (same engine as the API) |
| `evaluate` | compare a reconciliation run with ground truth (§12) |
| `export_data --out output/full` | dataset, ground truth, reports, manifests (§11.4) |
| `verify_reproducibility` | generate twice into temp dirs, compare row counts + SHA-256 per file |
| `demo_workflow` | run the 11-step API scenario |

### 11.2 Pipeline (parents → children; each stage reads only rows already built)

```
 1 Programs (20) → 2 Terms (3 billable + 9 historical) → 3 FeeSchedules (V1 + V2 revisions)
 → 4 ScholarshipTypes (7) + bridge → 5 Students (10,000) → 6 Enrollments (30,000) + events
 → 7 Invoices (30,000) + Lines (+ waivers) + CHARGE ledger
 → 8 Eligibility (210,000) → Scholarships (15,000; 300–600 full-ride) → ScholarshipAllocations + ledger
 → 9 Payments (50,000: full / partial / over; SELF, GUARDIAN, SPONSOR) + ledger → PaymentAllocations
 → 10 Late fees → 11 Withdrawals / cancellations (adjust, release scholarship then payments)
 → 12 Refunds (withdrawal + overpayment) → 13 Collection-status refresh @ 2026-12-31
 → 14 Inject transactional anomalies (4 types) + GroundTruthLabel
 → 15 BalanceSnapshots (term ends + 2026-12-31) → 16 Inject balance mismatches + GroundTruthLabel
 → 17 ID materialization + bulk insert → 18 Reconciliation run (blind) → 19 Evaluation → 20 Export
```

- **Exact baseline counts.** The generator hits the brief's numbers exactly: 30,000 invoices (3 per student); 15,000 awards (5,000 per term, given to eligible candidates in a fixed ranking: full-ride first, then type priority, GPA desc, need desc, student_id); 50,000 payments (per-invoice instalment counts sampled, then adjusted deterministically to total exactly 50,000). Eligible-but-not-awarded counts are reported in the validation summary. The quota is a generation policy only; the API doesn't apply it.
- **Snapshots after transactional injection.** Stage 15 runs after stage 14, so duplicate-invoice and refund corruption is already in the ledger the snapshots read. Only stage 16 creates balance mismatches. Each anomaly type therefore produces its own finding, not a knock-on balance mismatch.
- **ID materialization.** Records are built in memory with temporary keys. IDs are assigned once, in stage 17, in (business date, deterministic tiebreak) order across baseline *and* injected rows. Injected records therefore draw from the same sequences and don't stand out by ID position. IDs never change after insert.
- **Boundary cases** (valid, not anomalies) are planted by quota and counted in the manifest: payment exactly equal to outstanding; overpayment of ₹0.01; withdrawals on days 0/7/8/14/15/27/28; late fee at ₹500 minimum and ₹5,000 maximum; scholarship exactly at cap; full-ride covering registration; fully waived line; cancellation with registration still owed.
- **Distributions** (in `generation.yaml`): GPA truncated normal (μ 3.0, σ 0.5, 0–4); need index Beta(2, 3); 15% international; credits uniform in program range; payment dates skewed toward the due date with a late tail; payer mix ~70% SELF / 20% GUARDIAN / 10% SPONSOR. The validation summary reports actual distributions (mean, p10/p50/p90) so they can be reviewed.

### 11.3 Deterministic randomness

```python
import hashlib, random


def stage_rng(seed: int, stage_name: str) -> random.Random:
    digest = hashlib.sha256(f"{seed}:{stage_name}".encode("utf-8")).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))
```

Each stage gets its own sub-stream, so changing one stage doesn't shift the others. Python's built-in `hash()` is never used for seeding: string hashing is randomized per process (`PYTHONHASHSEED`), so it would give different streams on each run. `tests/unit/test_stage_rng.py` pins golden values and runs a subprocess under different `PYTHONHASHSEED` values. Business dates come from the term calendar, never the wall clock.

### 11.4 Deterministic exports vs execution metadata

```
output/<scale>/
├── dataset/                 ← deterministic, checksummed
│   ├── csv/<table>.csv      sorted by PK, fixed column order, UTF-8, LF, money as "1234.50"
│   ├── json/<table>.json    same rows; sorted keys; 2-space indent; LF
│   └── sql/schema.sql, sql/seed.sql   schema from sqlite_master sorted by name; INSERTs in PK order
├── ground_truth/            ← deterministic, checksummed, separate from dataset
│   └── ground_truth.csv, ground_truth.json
├── reports/                 ← deterministic, checksummed
│   └── findings.csv, validation_summary.md, evaluation_report.json
├── dataset_manifest.json    ← deterministic: seed, scale, config_hash, row counts, coverage counts,
│                              SHA-256 per file above + combined digest
├── source_manifest.json     ← deterministic: provenance (100% synthetic, no external sources)
├── run_log.json             ← NOT deterministic, NOT checksummed: started/finished timestamps,
│                              durations, host, Python/Django/SQLite versions, git SHA
└── reproducibility_report.md  run A vs run B: counts + per-file hashes (timestamps only from run_log)
```

- Every table has a `recorded_at` system timestamp. It's **excluded from exports**, as are `GenerationRun.started_at/finished_at` and `ReconciliationRun.started_at/finished_at`. All exported dates are business dates.
- `config_hash` = SHA-256 of the canonical JSON of the resolved config (sorted keys, no whitespace).
- The SQLite files themselves are never checksummed, because their page layout isn't deterministic.
- `verify_reproducibility` passes when both runs have identical row counts and identical SHA-256 for every checksummed file.

Scale: `starter` = 1% of transactional volume (master data stays at full size: 20 programs, 12 terms, 7 types). Anomaly counts scale too, with a minimum of 3 per type.

---

## 12. Reconciliation (detector) and evaluation (blind to ground truth)

### 12.1 Detector checks
`run_reconciliation(scope, as_of)` runs these check families. Each writes `AnomalyFinding` rows and per-check counts into `ReconciliationRun.summary`:

| Family | Checks |
|---|---|
| Structural | PK uniqueness, FK orphans, enum validity, ID format, ID counter gaps/duplicates |
| Relationship | join consistency: invoice ↔ enrollment student/term; line fee schedule ↔ invoice program/term/residency; allocation line ↔ payment student; award ↔ invoice student/term and eligibility; refund ↔ payment student; enrollment program = student program (`RELATIONSHIP_MISMATCH`) |
| Temporal | enroll ≤ issue ≤ due; payment ≥ first issue; allocation ≥ payment date; withdrawal within term; fee-version chain order |
| Lifecycle | every event chain uses only allowed transitions; stored status = last event; collection status = BR-INV-06 at `collection_status_as_of` |
| Arithmetic | `net = gross − waiver`, `gross = qty × unit`, invoice total = Σ FEE lines, line open ≥ 0, adjustment ≤ line, ledger completeness and sign (BR-LED-01 1:1), all in `Decimal` |
| **Required anomalies** | `PAYMENT_OVER_ALLOCATED` (BR-ALC-01), `SCHOLARSHIP_EXCEEDS_ELIGIBLE_FEES` (BR-SCH-04/08), `DUPLICATE_INVOICE` (BR-INV-01), `REFUND_EXCEEDS_NET_PAID` (BR-REF-01), `BALANCE_LEDGER_MISMATCH` (BR-BAL-03) |
| Reconciliation | BR-BAL-02 identity per student; % of students reconciled |

### 12.2 Isolation of ground truth (enforced, not by convention)
1. **Separate database.** `GroundTruthLabel` lives only in `ground_truth.sqlite3`. `GroundTruthRouter` sends that model to the `ground_truth` alias, allows migrating it only there, and refuses cross-database relations. The table doesn't exist in `db.sqlite3`, and the detector only ever uses the `default` connection.
2. **Import rule.** No module in `students`, `academics`, `billing`, `scholarships`, `payments`, `reconciliation` or `core` may import `datagen` or name the `ground_truth` alias. `tests/architecture/test_ground_truth_isolation.py` checks this with an AST scan.
3. **No label leakage in data.** Domain tables have no `is_anomaly` or injection columns. Injected rows use the same ID sequences (§11.2), plausible business dates, and ordinary enum values.
4. **Only `datagen.evaluation`** reads both findings (default DB) and labels (ground_truth DB). It's run by the `evaluate` command and the admin-only evaluation endpoint, and it writes only `evaluation_report.json`.
5. **Separate files.** Exports keep `ground_truth/` apart from `dataset/`.

### 12.3 Evaluation metrics
Findings and labels are joined on `(issue_type, record_id)`. Per required issue type: TP, FP, FN, precision, recall, F1. A finding of another type on a record listed in some label's `related_ids` counts as **consequential**, not a false positive. Any other finding on clean data is **unexpected**. Targets: recall = 1.0 and precision = 1.0 for each of the 5 types, 0 unexpected findings, and 100% of non-anomalous students reconciled (BR-BAL-02).

---

## 13. Actors, authentication and roles

DRF token auth plus session auth (for the browsable API and Swagger). The actors in the brief become Django groups:

| Actor (brief) | Group | Can |
|---|---|---|
| Learner | `learner` | read own student record, statement, balance |
| Instructor | `instructor` | read enrollments for their programs (no finance data) |
| Academic administrator | `academic_admin` | programs, terms, enrollments, withdrawals, cancellations |
| Support staff | `support_staff` | read everything, request refunds |
| Platform administrator | `platform_admin` | invoices, fee schedules, scholarships, payments, refund approval, reconciliation, datagen, evaluation |

`python manage.py seed_roles` creates the groups and demo users.

---

## 14. Testing strategy

Test files committed now are written against the calculator contracts in §5. **They have not been run against the project, because no implementation exists yet.** They are expected to fail with `ImportError` until each module is built, and they become the acceptance tests for those phases.

| File (exists now) | Covers | Turns green in |
|---|---|---|
| `tests/unit/test_withdrawal_refund_pct.py` | BR-WDR-02 days 7/8/14/15/27/28/29, census = day 28 | Phase 5 |
| `tests/unit/test_invoice_status.py` | BR-INV-04/05/06: every collection status, zero-balance cases, due-date boundary, guardian/sponsor payments, adjustments kept separate, negatives rejected | Phase 2 |
| `tests/unit/test_invoice_lifecycle.py` | BR-INV-08: lifecycle transitions, terminal states, void preconditions | Phase 2 |
| `tests/unit/test_full_ride_scholarship.py` | BR-SCH-06/08: coverage by scope, award amount and cap | Phase 3 |
| `tests/unit/test_payment_allocation.py` | BR-ALC-01..04: AUTO order, line caps, remainder, invalid input | Phase 4 |
| `tests/unit/test_withdrawal_release.py` | BR-WDR-03 / BR-SCH-07: scholarship first, then payments LIFO, ROUND_HALF_UP, sweep proving S − R ≤ N', full-ride, cancellation, zero adjustment | Phase 5 |
| `tests/unit/test_worked_example.py` | §7 numbers end to end, BR-LED-01 signs, BR-BAL-02 identity at each stage, refund caps | Phase 5 |
| `tests/unit/test_fee_schedule_versions.py` | BR-FEE-01/03: version in force, derived windows, inputs not mutated | Phase 2 |
| `tests/unit/test_stage_rng.py` | §11.3 golden values; independence from `PYTHONHASHSEED` | Phase 7 |
| `tests/architecture/test_ground_truth_isolation.py` | §12.2 import rule | Phase 6 |

Planned suites, written in their phases:

| Suite | What it proves |
|---|---|
| `services/` | rules + atomicity (failure partway → nothing written), BR-ALC-06, withdrawal orchestration, concurrency on SQLite (§8.2) |
| `api/` | one test per endpoint and per error code; error envelope; permissions; 405 on financial edits |
| `workflow/` | the 11-step scenario; final balances equal §7-style hand calculations |
| `datagen/` | starter scale: exact baseline counts, all structural/relationship checks clean, P = R = 1 per type, 0 unexpected findings, two runs give identical checksums, distributions inside configured bands, parity with services |

Rule IDs appear in test names (`test_BR_ALC_01_…`). Target ≥ 90% line coverage on `calculators.py` and `services.py`. A test counts as passing only after it has run green against the real implementation in CI or locally. Results go in `validation_summary.md`.

---

## 15. Requirement traceability matrix

Every requirement in the YottaFlex brief, with where it's satisfied and how it's verified.

| ID | Requirement (brief) | Where satisfied | Verified by |
|---|---|---|---|
| R-01 | Calculate tuition and fees | BR-FEE-01..04, BR-WAV-01, `price_lines` | unit + workflow tests; term-fee check |
| R-02 | Apply scholarship eligibility | BR-SCH-01..08, Eligibility (210,000) | `test_full_ride_scholarship`, services |
| R-03 | Apply payments | BR-PAY-01..03, BR-ALC-01..06 | `test_payment_allocation`, API |
| R-04 | Reconcile student balances | BR-LED-01, BR-BAL-01..03, §7 | `test_worked_example`, reconciliation |
| R-05 | Expose key decisions and exceptions | Eligibility reason codes + input snapshot; Refund `calculation`; release plans; error envelope with rule_id; AnomalyFinding | API tests; findings export |
| R-06 | Preserve traceability | P2/P5; one ledger entry per source row; status events; fee-version chain; `source_ref` | lifecycle + ledger-completeness checks |
| R-07 | Produce measurable outputs / meaningful evaluation metrics | §12.3 metrics; validation summary; coverage counts | `evaluation_report.json` |
| R-08 | Medium complexity: multiple entities, conditional rules, ≥ 1 exception path | 27 tables (26 in the domain DB + GroundTruthLabel); conditional rules in §6; 5 anomaly types + 18 error codes | — |
| R-09 | Primary actors: learner, instructor, academic admin, support staff, platform admin | §13 groups | API permission tests |
| R-10 | Workflow 1: generate master data first (Student, Program, FeeSchedule, Enrollment, Invoice) | §11.2 stages 1–7 | datagen tests |
| R-11 | Workflow 2: dependent records use valid parent IDs | FK + PROTECT; stage order; Relationship checks | structural/relationship findings = 0 |
| R-12 | Workflow 3: fee schedules by program/term | `FeeSchedule` (program, term, residency, versioned) | `test_fee_schedule_versions` |
| R-13 | … create enrollments | `Enrollment` + events | services/API |
| R-14 | … generate invoices from eligible fee lines | `generate_invoice`, BR-FEE-01/04 | workflow step 3 |
| R-15 | … define scholarship criteria and awards | `ScholarshipType`, `Scholarship` | unit + API |
| R-16 | … allocate payments to invoice lines | `PaymentAllocation` (N:M bridge) | `test_payment_allocation` |
| R-17 | … calculate balances and late fees | BR-BAL-01, BR-LATE-01 | unit (late fee min/max/zero) |
| R-18 | … handle cancellations and refunds | BR-WDR-01..03, BR-REF-01..04 | `test_withdrawal_release`, `test_worked_example` |
| R-19 | Workflow 4: persist state changes as records/events; don't overwrite history | append-only tables; 5 event tables; immutable fee versions; adjustment lines | lifecycle checks; 405 on edits |
| R-20 | Workflow 5: outcomes and labels from documented rules, not random | calculators decide outcomes; injector writes labels | parity test; evaluation |
| R-21 | 11 core entities | DATABASE_DESIGN §3 | migrations |
| R-22 | PK for every entity, FK for every relationship | DATABASE_DESIGN §3–§4 (documented exceptions: polymorphic `record_id`, cross-DB label refs) | structural checks |
| R-23 | Cardinality, optional relationships, effective dates, lifecycle states | DATABASE_DESIGN §4–§6 | lifecycle checks |
| R-24 | Bridge tables for many-to-many | ScholarshipTypeProgram, ScholarshipAllocation, PaymentAllocation | — |
| R-25 | Retain event history for state changes | *Event tables; InvoiceEvent dimension | lifecycle checks |
| R-26 | Validate all joins | Relationship check family §12.1 | findings = 0 on clean data |
| R-27 | Anomalies labelled; unrelated rows not corrupted | injector touches only labelled records; snapshots after injection (§11.2) | 0 unexpected findings |
| R-28 | ID conventions STU / TERM / INV-STU / SCH / PAY / ALLOC | DATABASE_DESIGN §2; calendar keeps all in 2026 | ID-format check |
| R-29 | Zero-padded, immutable IDs, consistent across files | `core.ids`; materialization once; same IDs in CSV/JSON/SQL | export cross-file check |
| R-30 | Step 1: data dictionary (type, required, allowed values, units, uniqueness, validation) | `DATA_DICTIONARY.md` generated from models + DATABASE_DESIGN §3 | — |
| R-31 | Step 2: parents before children | §11.2 | — |
| R-32 | Step 3: deterministic IDs and naming | §11.2 ID materialization | reproducibility |
| R-33 | Step 4: 10k students, 20 programs, 30k invoices, 15k awards, 50k payments | exact baseline counts (DATABASE_DESIGN §7) | manifest row counts |
| R-34 | … partial payments, overpayments, fee waivers, term withdrawals | coverage targets (DATABASE_DESIGN §7) | manifest coverage counts |
| R-35 | … fixed seed 20261022; parameterized scale, same generator | `--seed`, `--scale`; `generation.yaml` | starter + full from same command |
| R-36 | Step 5: valid baseline first, then labelled edge cases (5 types) | stages 14/16; GroundTruthLabel | evaluation P/R |
| R-37 | Step 6: derived values computed from source records | P3; calculators | arithmetic checks |
| R-38 | Step 7: separate ground truth; never leaked into inputs | §12.2 (separate DB, import rule, separate files) | `test_ground_truth_isolation` |
| R-39 | Step 8: validation of PK, FK, enums, date order, arithmetic, lifecycle, duplicates | §12.1 families | `validation_summary.md` |
| R-40 | Step 9: CSV/JSON/SQL seed + README, schema/data dictionary, generator command, seed, source manifest, validation summary | §11.4 exports; README | export test |
| R-41 | Step 10: regenerate twice with same seed; compare counts/checksums | `verify_reproducibility` | `reproducibility_report.md` |
| R-42 | Acceptance: balances reconcile | BR-BAL-02 | 100% clean students reconciled |
| R-43 | Acceptance: scholarship rules enforced | BR-SCH-02..08 in services | API 422/409 tests |
| R-44 | Acceptance: allocation and refund logic auditable | allocation/deallocation rows, `rule_applied`, refund `calculation`, ledger links | statement endpoint; tests |
| R-45 | Acceptance: term-specific fees correct | BR-FEE-01/03; FALL +4% | arithmetic check: line unit = version in force |
| R-46 | Eval (a) realistic distributions, meaningful variation | §11.2 distributions + reported stats | validation summary |
| R-47 | Eval (b) consistent IDs, valid joins | R-26/R-28/R-29 | — |
| R-48 | Eval (c) dependency/lifecycle consistency | lifecycle family | — |
| R-49 | Eval (d) correct derived values and reconciliation | arithmetic + reconciliation families | — |
| R-50 | Eval (e) normal, boundary, failure, exception coverage | boundary quotas §11.2; API error tests; anomalies | manifest coverage |
| R-51 | Eval (f) separate ground truth | R-38 | — |
| R-52 | Eval (g) reproducibility, documentation, lawful provenance | §11.4; docs set; `source_manifest.json` | reproducibility report |
| R-53 | Public data: if external data used, record URL, version, date, license, attribution, transformations, discarded fields | No external data. `source_manifest.json` states this and keeps those fields with N/A. Emails use reserved `example.edu` | — |
| R-54 | **Added by owner:** Django/DRF/SQLite backend, workflow APIs, API validation, atomic transactions, API docs, tests | §5, §8–§10, §14 | API + workflow tests |

---

## 16. Implementation phases (each ends green: migrations applied, the phase's tests passing)

| Phase | Build | Exit criteria |
|---|---|---|
| 0 | Skeleton, settings (2 DBs + router), `core`, DRF + spectacular + pytest-django, SQLite version check | `/api/v1/health/`, `/api/docs/` work; ID tests pass |
| 1 | **students + academics**: models, migrations, serializers, endpoints | create student + enrollment via API; lifecycle tests |
| 2 | **billing**: fee versions, invoice generation, waivers, ledger, late fees, collection/lifecycle status, void | `test_invoice_status`, `test_invoice_lifecycle`, `test_fee_schedule_versions` green; workflow steps 1–3b |
| 3 | **scholarships**: types, eligibility, award, apply, caps, full-ride, BR-ALC-06 | `test_full_ride_scholarship` green; steps 4–5 |
| 4 | **payments**: record, allocate, deallocate, refunds | `test_payment_allocation` green; steps 6–9; concurrency test |
| 5 | Withdrawal/cancellation orchestration | `test_withdrawal_refund_pct`, `test_withdrawal_release`, `test_worked_example` green |
| 6 | **reconciliation**: snapshots, checks, findings, run API | `test_ground_truth_isolation` green; steps 10–11 on fixtures |
| 7 | **datagen**: pipeline at starter scale, injector, ground truth DB, evaluation | `test_stage_rng` green; starter: exact counts, P = R = 1, 0 unexpected |
| 8 | Full scale, exports, manifests, reproducibility | 2 runs identical; volumes met |
| 9 | Docs: README, DATA_DICTIONARY, API.md (captured examples), BUSINESS_RULES, validation summary; `demo_workflow` | a fresh clone runs end to end with 3 commands |

---

## 17. Decisions and assumptions log

| # | Decision | Reason |
|---|---|---|
| D1 | **Trimester calendar 2026**: SPRING 26 Jan–09 May, SUMMER 25 May–15 Aug, FALL 07 Sep–19 Dec. Invoice 21 days before start, due 7 days after. Every student is enrolled in all 3 terms → 30,000 invoices. **Every business date falls in 2026**, so every year-scoped ID carries `-2026-` as the conventions show. | Matches the ID examples; no `PAY-2025-…` from December prepayments |
| D2 | `INV-STU-<6-digit student number>-2026-<6-digit per-student-per-year sequence>`, e.g. `INV-STU-000001-2026-000002` | Avoids `INV-STU-STU-…` |
| D3 | "Scholarship" = an **award** (`SCH-2026-000001`). Criteria live in `ScholarshipType`. | The ID convention is per award |
| D4 | Scholarships reduce balances via `ScholarshipAllocation` + ledger credit, not as Payments | Keeps the 50,000 payments as real money |
| D5 | Late fees, adjustments, voids and withdrawals are new InvoiceLines, never edits | Append-only history |
| D6 | INR, 2 dp, `ROUND_HALF_UP`; GPA on a 4.00 scale | Explicit |
| D7 | Generation as-of date **2026-12-31**; no business event after it | Fixed and reproducible |
| D8 | Cross-row rules enforced in services, not DB constraints | Anomalies must be representable (§10) |
| D9 | Injected anomalies are **additional** labelled rows, except balance mismatches, which modify the labelled snapshot only | Baseline volumes stay exact |
| D10 | No external data | Simplest lawful provenance |
| D11 | 9 historical, non-billable terms (2023-SPRING … 2025-FALL) exist only as admit terms | `Student.admit_term` must be a real FK while all billing is in 2026 |
| D12 | Exactly 15,000 baseline awards through a ranked quota of 5,000 per term (generation policy only) | Meets the brief exactly and deterministically |
| D13 | Invoice status split into `lifecycle_status` + `collection_status` | Cancellation/void no longer hide or fake collection state |
| D14 | Ground truth in a separate SQLite database | Isolation is enforced, not a convention |
