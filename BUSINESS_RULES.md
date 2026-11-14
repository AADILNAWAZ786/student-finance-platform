# Business rules

The catalogue of business rules the backend enforces: every rule ID of ARCHITECTURE §6, what
it requires, where it is implemented, which service or endpoint applies it, and which tests
verify it. ARCHITECTURE §6 is the frozen summary and refers here for the full catalogue.

**Status** is `Implemented` only where the cited code and at least one cited test exist;
`tests/architecture/test_business_rules.py` checks that every rule of ARCHITECTURE §6 is listed,
that every cited function exists, and that every cited test file names the rule (or the cited
test exists). Nothing here is a rule the design does not state.

> Location: at the repository root at the project owner's instruction; the frozen
> ARCHITECTURE §3 layout places it under `docs/`.

How rules are enforced (ARCHITECTURE §5, §10):

1. **Pure calculators** (`<app>/calculators.py`) compute amounts, caps and statuses with
   `Decimal` and no database access; they are unit-tested in isolation.
2. **Services** (`<app>/services.py`) are the only code that writes. Each write is one atomic
   transaction that validates the rule, writes the rows, their events and ledger entries, or
   writes nothing and answers with an error envelope naming the rule (`rule_id`).
3. **Database constraints** hold the single-row rules (signs, enums, required-iff) that need no
   arithmetic.
4. **Reconciliation** (`reconciliation/checks/`) re-checks the rules on stored data and reports
   breaches as findings; it never changes data. Every service-layer rule is mapped to a check in
   [`docs/phase6_sv_coverage.md`](docs/phase6_sv_coverage.md).

Error codes: 400 malformed input; 404 unknown ID; 409 state conflicts (duplicates, illegal
transitions, group conflicts); 422 valid input that breaks a financial rule.

## Fees and invoicing

| Rule | Requirement | Implementation | Applied by | Tests | Status |
|---|---|---|---|---|---|
| BR-FEE-01 | An enrollment is priced from the fee-schedule versions for its program, term and residency (or `ALL`) in force on the term's invoice date; none in force → 422 `FEE_SCHEDULE_NOT_FOUND`. | `billing.calculators.resolve_fee_version`, `billing.services.resolve_fees` | `billing.services.generate_invoice`; `POST /invoices/generate/` | `tests/unit/test_fee_schedule_versions.py`, `tests/services/test_services_billing.py` | Implemented |
| BR-FEE-02 | `PER_CREDIT` lines: quantity = credits; `FLAT`: quantity 1; gross = quantity × unit. | `billing.calculators.price_lines` | `billing.services.generate_invoice` | `tests/unit/test_billing_calculators.py` | Implemented |
| BR-FEE-03 | Fee-schedule rows are immutable; a price change inserts version n+1 that supersedes n and starts later; windows are derived, never stored, never overlap. | `billing.services.new_fee_schedule_version`, `billing.calculators.effective_windows` | `POST /fee-schedules/{id}/new-version/`, `GET /fee-schedules/{id}/history/` | `tests/unit/test_fee_schedule_versions.py`, `tests/services/test_services_fee_schedules.py` | Implemented |
| BR-FEE-04 | LAB lines only when the program has a lab; INTERNATIONAL_SERVICE only for international students. | `billing.policy.applicable_fee_types`, `billing.calculators.price_lines` | `billing.services.generate_invoice` | `tests/unit/test_billing_calculators.py::test_BR_FEE_04_lab_and_international_service_only_when_applicable`, `tests/services/test_services_billing.py::test_domestic_student_never_gets_international_service_fee` | Implemented |
| BR-WAV-01 | A waiver reduces one waivable line: 0 ≤ waiver ≤ gross, a reason when > 0; net = gross − waiver. | `billing.calculators.apply_waivers` | `billing.services.generate_invoice` (`waivers`) | `tests/unit/test_billing_calculators.py`, `tests/services/test_services_billing.py` | Implemented |
| BR-INV-01 | At most one invoice not `VOID` per enrollment; a second → 409 `DUPLICATE_INVOICE`. | `billing.services.generate_invoice` | `POST /invoices/generate/` | `tests/services/test_services_billing.py`, `tests/api/test_api_billing.py` | Implemented |
| BR-INV-02 | `total_amount` = Σ net of the FEE lines at issue; never changed. | `billing.services.generate_invoice` | same | `tests/services/test_services_billing.py`, `tests/verification/test_verify_billing.py` | Implemented |
| BR-INV-03 | Issue date = term invoice date (or `enrolled_on` if later); due date = term due date. | `billing.services.generate_invoice` | same | `tests/services/test_services_billing.py` | Implemented |
| BR-INV-04 | Invoice amounts are computed, never stored: original charges, adjustments, net charges, scholarship applied, amount paid (by payer), outstanding; a negative value is an arithmetic error. | `billing.calculators.invoice_amounts`, `billing.calculators.check_amounts` | every invoice read; `GET /invoices/{id}/` | `tests/unit/test_invoice_status.py::test_adjustments_are_not_counted_as_payment_or_scholarship`, `tests/unit/test_invoice_status.py::test_negative_outstanding_raises`, `tests/unit/test_worked_example.py` | Implemented |
| BR-INV-05 | Paid (money from payments) is distinct from settled (outstanding 0 by any means); scholarship and adjustments are never money paid. | `billing.calculators.invoice_amounts` (`is_settled`) | invoice, statement and balance views | `tests/unit/test_invoice_status.py::test_full_ride_with_no_payment_is_scholarship_covered`, `tests/unit/test_invoice_status.py::test_sponsor_paid_invoice_is_paid_not_scholarship_covered` | Implemented |
| BR-INV-06 | Collection status, first match wins: OVERDUE, PARTIALLY_PAID, UNPAID while outstanding > 0; ADJUSTED_TO_ZERO, PAID, SCHOLARSHIP_COVERED at 0. | `billing.calculators.collection_status` | `billing.services.recalculate_collection_status` | `tests/unit/test_invoice_status.py::test_day_after_due_date_unpaid_becomes_overdue`, `tests/unit/test_invoice_status.py::test_scholarship_covered_is_never_overdue`, `tests/unit/test_invoice_status.py::test_zero_from_adjustments_alone_is_adjusted_to_zero_not_scholarship_covered`, `tests/verification/test_verify_scholarships.py` | Implemented |
| BR-INV-07 | Collection status recalculated in the same transaction as every change, with a `COLLECTION` event and its as-of date; a refresh moves past-due invoices to OVERDUE. | `billing.services.recalculate_collection_status`, `billing.services.refresh_collection_status` | `POST /billing/collection-status/refresh/` | `tests/services/test_services_billing.py` | Implemented |
| BR-INV-08 | Lifecycle ISSUED → CANCELLED or VOID (terminal); void only with nothing allocated (409 `INVOICE_HAS_ALLOCATIONS`) and posts VOID adjustments. | `billing.services.void_invoice`, `billing.calculators.void_adjustments` | `POST /invoices/{id}/void/` | `tests/unit/test_invoice_lifecycle.py`, `tests/services/test_services_billing.py`, `tests/api/test_api_billing.py` | Implemented |
| BR-LATE-01 | An ISSUED invoice still outstanding at the end of due date + 14 grace days gets one LATE_FEE line the next day: 2 % of outstanding, min ₹500, max ₹5,000; never scholarship-eligible, waivable or refundable. | `billing.calculators.late_fee`, `billing.calculators.late_fee_date`, `billing.services.assess_late_fees` | `POST /billing/late-fees/assess/` | `tests/unit/test_billing_calculators.py`, `tests/services/test_services_billing.py` | Implemented |

## Scholarships

| Rule | Requirement | Implementation | Applied by | Tests | Status |
|---|---|---|---|---|---|
| BR-SCH-01 | Eligibility evaluated per student × type × term against the type's criteria (GPA, need, program, residency, first term, effective window); every evaluation stored with inputs and reason codes. | `scholarships.calculators.evaluate`, `scholarships.services.evaluate_eligibility` | `POST /scholarships/eligibility-checks/` | `tests/unit/test_scholarship_calculators.py`, `tests/services/test_services_eligibility.py` | Implemented |
| BR-SCH-02 | An award needs a stored eligible evaluation of the same student, type and term; at most one award per evaluation. | `scholarships.services.award_scholarship` | `POST /scholarships/` | `tests/services/test_services_scholarships.py`, `tests/api/test_api_scholarships.py` | Implemented |
| BR-SCH-03 | Award = percent × eligible base or fixed, capped by the type's maximum per term. | `scholarships.calculators.award_amount` | `scholarships.services.award_scholarship` | `tests/unit/test_scholarship_calculators.py`, `tests/services/test_services_scholarships.py` | Implemented |
| BR-SCH-04 | Σ scholarship applied to an invoice ≤ its eligible fees; an over-cap request → 422 `SCHOLARSHIP_EXCEEDS_ELIGIBLE_FEES`. | `scholarships.calculators.eligible_fee_base`, `scholarships.services.reserved_on_invoice`, `scholarships.services.apply_scholarship` | `POST /scholarships/`, `POST /scholarships/{id}/apply/` | `tests/unit/test_scholarship_calculators.py`, `tests/services/test_services_scholarships.py` | Implemented |
| BR-SCH-05 | At most one award per exclusive group per term (409 `SCHOLARSHIP_GROUP_CONFLICT`). | `scholarships.services.award_scholarship` | `POST /scholarships/` | `tests/services/test_services_scholarships.py` | Implemented |
| BR-SCH-06 | Scholarships cover only scholarship-eligible lines, in allocation-priority order. | `scholarships.calculators.coverable_lines`, `scholarships.calculators.plan_application` | `scholarships.services.apply_scholarship` | `tests/unit/test_scholarship_calculators.py`, `tests/unit/test_full_ride_scholarship.py` | Implemented |
| BR-SCH-07 | On withdrawal, applied scholarship is reversed by the same share as the line's adjustment. | `scholarships.calculators.split_reversal`, `scholarships.services.reverse_for_adjustment` | `POST /enrollments/{id}/withdraw/` | `tests/unit/test_withdrawal_release.py`, `tests/services/test_services_withdrawals.py` | Implemented |
| BR-SCH-08 | Full ride (SCT-007) is the only type covering all FEE lines (never LATE_FEE or ADJUSTMENT), is awarded first and excludes every other award in the term (409). | `scholarships.services.is_full_ride`, `scholarships.calculators.coverable_lines` | `POST /scholarships/`, `POST /scholarships/{id}/apply/` | `tests/unit/test_full_ride_scholarship.py`, `tests/services/test_services_scholarships.py` | Implemented |

## Payments and allocation

| Rule | Requirement | Implementation | Applied by | Tests | Status |
|---|---|---|---|---|---|
| BR-PAY-01 | Payment amount > 0; `external_reference` unique (409 `DUPLICATE_PAYMENT`). | `payments.services.record_payment` | `POST /payments/` | `tests/services/test_services_payments.py`, `tests/api/test_api_payments.py` | Implemented |
| BR-PAY-02 | A payment is credited to the ledger in full on receipt; allocation posts nothing. | `payments.services.record_payment`, `billing.services.post_payment_ledger` | `POST /payments/` | `tests/services/test_services_payments.py::test_allocations_post_nothing_to_the_ledger` | Implemented |
| BR-PAY-03 | Every payment counts as money paid, whatever the payer (SELF, GUARDIAN, SPONSOR) or method. | `billing.calculators.invoice_amounts` (`payments_by_payer`) | invoice and balance views | `tests/services/test_services_payments.py::test_BR_PAY_03_every_payer_counts_as_money_paid`, `tests/unit/test_invoice_status.py::test_guardian_and_sponsor_payments_count_as_paid` | Implemented |
| BR-ALC-01 | Σ net allocations of a payment ≤ amount − its refunds not rejected (422 `ALLOCATION_EXCEEDS_PAYMENT`). | `payments.calculators.payment_available`, `payments.services.allocate_payment` | `POST /payments/{id}/allocate/` | `tests/unit/test_payment_allocation.py`, `tests/services/test_services_payments.py`, `tests/api/test_api_payments.py` | Implemented |
| BR-ALC-02 | Allocations only to FEE / LATE_FEE lines and never beyond the line's open amount (422 `ALLOCATION_EXCEEDS_LINE_OPEN`); open ≥ 0 always. | `billing.calculators.line_open_amounts`, `payments.services.allocate_payment` | `POST /payments/{id}/allocate/`, `POST /scholarships/{id}/apply/` | `tests/unit/test_billing_calculators.py`, `tests/services/test_services_payments.py` | Implemented |
| BR-ALC-03 | AUTO order: oldest due date first, then line priority (tuition first, late fee last), then line ID. | `payments.calculators.plan_allocation` | `POST /payments/{id}/allocate/` (`AUTO`), `auto_allocate` on `POST /payments/` | `tests/services/test_services_payments.py::test_auto_oldest_due_first_then_priority_and_status_partially_paid`, `tests/services/test_services_payments.py::test_auto_crosses_invoices_oldest_due_date_first` | Implemented |
| BR-ALC-04 | Any remainder stays unallocated credit, to allocate later or refund. | `payments.calculators.plan_allocation`, `payments.calculators.unallocated_credit` | same | `tests/unit/test_payment_allocation.py::test_BR_ALC_04_remainder_stays_unallocated`, `tests/services/test_services_payments.py::test_BR_ALC_04_overpayment_remainder_stays_unallocated` | Implemented |
| BR-ALC-05 | Allocations are never edited; a DEALLOCATE row (negative) references the original, Σ ≤ original. | `payments.services.deallocate` | withdrawal, cancellation and scholarship-precedence releases | `tests/services/test_services_payments.py`, `tests/verification/test_verify_payments.py` | Implemented |
| BR-ALC-06 | Scholarship precedence: applying a scholarship to a line covered by payments deallocates them last-in-first-out (`SCHOLARSHIP_PRECEDENCE`), freeing credit. | `payments.calculators.plan_release_lifo`, `payments.services.release_for_scholarship`, `billing.services.release_payments_for_scholarship` | `POST /scholarships/{id}/apply/` | `tests/unit/test_payment_calculators.py`, `tests/services/test_services_payments.py`, `tests/services/test_services_scholarships.py` | Implemented |

## Withdrawals, cancellations and refunds

| Rule | Requirement | Implementation | Applied by | Tests | Status |
|---|---|---|---|---|---|
| BR-WDR-01 | Cancellation before term start: every line except REGISTRATION credited in full; enrollment and invoice → CANCELLED; the invoice's collection status stays live on what is still owed. | `academics.services.cancel_enrollment`, `billing.calculators.cancellation_adjustments`, `billing.withdrawals.apply_enrollment_exit` | `POST /enrollments/{id}/cancel/` | `tests/unit/test_withdrawal_calculators.py`, `tests/services/test_services_withdrawals.py` | Implemented |
| BR-WDR-02 | Withdrawal on or after start, day d from the start: refundable lines (TUITION, LAB) credited 80 % (d 0–7), 60 % (8–14), 40 % (15–27), 0 % (≥ 28, the census date). | `academics.calculators.withdrawal_refund_pct`, `academics.calculators.withdrawal_day`, `billing.calculators.withdrawal_adjustments`, `academics.services.withdraw_enrollment` | `POST /enrollments/{id}/withdraw/` | `tests/unit/test_withdrawal_refund_pct.py::test_BR_WDR_02_refund_pct_boundaries`, `tests/services/test_services_withdrawals.py`, `tests/api/test_api_withdrawals.py` | Implemented |
| BR-WDR-03 | When an adjustment is posted on a line: reverse scholarship first (proportional share), then deallocate payments last-in-first-out; freed money becomes credit. | `billing.calculators.release_for_adjustment`, `payments.services.release_for_adjustment`, `scholarships.services.reverse_for_adjustment` | withdraw and cancel endpoints | `tests/unit/test_withdrawal_release.py`, `tests/services/test_services_withdrawals.py` | Implemented |
| BR-REF-01 | Refund ≤ net paid (Σ payments − Σ refunds not rejected), else 422 `REFUND_EXCEEDS_NET_PAID`. | `payments.calculators.net_paid`, `payments.calculators.refund_caps`, `payments.services.request_refund` | `POST /refunds/`, approve, process | `tests/unit/test_payment_calculators.py`, `tests/services/test_services_refunds.py` | Implemented |
| BR-REF-02 | Refund ≤ the credit balance less pending refunds (422 `REFUND_EXCEEDS_CREDIT_BALANCE`) and ≤ the source payment's available amount (422 `REFUND_EXCEEDS_PAYMENT_AVAILABLE`); pending refunds reserve their amount. | `payments.calculators.credit_available`, `payments.calculators.pending_reservations`, `payments.calculators.refund_caps` | `POST /refunds/`, approve, process; `GET /students/{id}/refundable/` | `tests/unit/test_payment_calculators.py`, `tests/services/test_services_refunds.py`, `tests/api/test_api_payments.py` | Implemented |
| BR-REF-03 | Lifecycle REQUESTED → APPROVED → PROCESSED, or → REJECTED; the ledger is debited only on PROCESSED; anything else → 409 `ILLEGAL_TRANSITION`. | `payments.services.approve_refund`, `payments.services.process_refund`, `payments.services.reject_refund` | `POST /refunds/{id}/approve/`, `/process/`, `/reject/` | `tests/services/test_services_refunds.py`, `tests/api/test_api_payments.py` | Implemented |
| BR-REF-04 | A refund goes back to the payer of its source payment (`payer_type` copied). | `payments.services.request_refund` | `POST /refunds/` | `tests/services/test_services_refunds.py`, `tests/workflow/test_end_to_end.py` | Implemented |

## Ledger, balances and reconciliation

| Rule | Requirement | Implementation | Applied by | Tests | Status |
|---|---|---|---|---|---|
| BR-LED-01 | Each source row posts exactly one ledger entry with the documented sign (charges +, credits −); payment allocations post none. | `billing.calculators.ledger_amount`, `billing.services.post_ledger`, `billing.services.post_scholarship_ledger`, `billing.services.post_payment_ledger`, `billing.services.post_refund_ledger` | every financial write | `tests/unit/test_worked_example.py::test_BR_LED_01_ledger_signs`, `tests/services/test_services_billing.py`, `tests/services/test_services_payments.py` | Implemented |
| BR-BAL-01 | Ledger balance at a date = Σ entries dated on or before it (negative = credit). | `billing.calculators.ledger_balance`, `billing.services.student_balance` | `GET /students/{id}/balance/` | `tests/unit/test_billing_calculators.py`, `tests/services/test_services_billing.py` | Implemented |
| BR-BAL-02 | Per student: ledger balance = Σ invoice outstanding − unallocated credit. | `reconciliation.calculators.balance_identity`, `billing.services.student_balance` | `GET /students/{id}/balance/` (`identity`); reconciliation | `tests/services/test_services_reconciliation.py`, `tests/services/test_services_payments.py`, `tests/api/test_api_billing.py` | Implemented |
| BR-BAL-03 | A balance snapshot's reported balance must equal BR-BAL-01 at its date; a mismatch is a `BALANCE_LEDGER_MISMATCH` finding. | `reconciliation.services.take_snapshot`, `reconciliation.checks.required.balance_ledger_mismatch` | `POST /balance-snapshots/`; reconciliation runs | `tests/verification/test_verify_reconciliation_anomalies.py`, `tests/datagen/test_injection.py` | Implemented |

### The five required anomaly types (detector)

The detector re-checks the rules above on stored data. These five checks find the challenge's
required edge cases; the generator injects and labels them (DATA_DICTIONARY §10) and the
evaluation compares findings with the labels.

| Anomaly (challenge wording) | Issue type | Rule | Check | Recommended action | Tests | Status |
|---|---|---|---|---|---|---|
| Payment allocated beyond amount | `PAYMENT_OVER_ALLOCATED` | BR-ALC-01 | `reconciliation.checks.required.payment_over_allocated` | `REJECT_ALLOCATION` | `tests/verification/test_verify_reconciliation_anomalies.py`, `tests/datagen/test_injection.py` | Implemented |
| Scholarship exceeds eligible fees | `SCHOLARSHIP_EXCEEDS_ELIGIBLE_FEES` | BR-SCH-04 (BR-SCH-08 for full ride) | `reconciliation.checks.required.scholarship_exceeds_eligible_fees` | `CAP_SCHOLARSHIP` | `tests/verification/test_verify_reconciliation_anomalies.py`, `tests/datagen/test_injection.py` | Implemented |
| Invoice generated twice | `DUPLICATE_INVOICE` | BR-INV-01 | `reconciliation.checks.required.duplicate_invoice` | `VOID_DUPLICATE` | `tests/verification/test_verify_reconciliation_anomalies.py`, `tests/datagen/test_injection.py` | Implemented |
| Refund exceeds net paid | `REFUND_EXCEEDS_NET_PAID` | BR-REF-01 | `reconciliation.checks.required.refund_exceeds_net_paid` | `REVERSE_REFUND` | `tests/verification/test_verify_reconciliation_anomalies.py`, `tests/datagen/test_injection.py` | Implemented |
| Balance disagrees with ledger | `BALANCE_LEDGER_MISMATCH` | BR-BAL-03 | `reconciliation.checks.required.balance_ledger_mismatch` | `RESTATE_BALANCE` | `tests/verification/test_verify_reconciliation_anomalies.py`, `tests/datagen/test_injection.py` | Implemented |

Beyond these five, the detector runs 63 checks in six families (structural, relationship,
temporal, lifecycle, arithmetic, required) and re-checks every service-layer rule; the mapping of
each rule to its check, issue type and test is
[`docs/phase6_sv_coverage.md`](docs/phase6_sv_coverage.md), kept true to the code by
`tests/architecture/test_sv_coverage_matrix.py`. Where stored data cannot fully re-check a rule
(P6-O1, P6-O13, P6-O14 in [`docs/KNOWN_LIMITATIONS.md`](docs/KNOWN_LIMITATIONS.md)), the
detector checks the parts it can and the limitation is documented.

## Other enforced validations (no BR number)

Service-layer rules from DATABASE_DESIGN §3 that carry an error code rather than a BR number:

| Entity | Rule | Implementation | Error | Tests |
|---|---|---|---|---|
| Student | age 16–60 on the admit term's start; program active; email on the synthetic domain | `students.services.create_student` | 400 | `tests/services/test_services_students.py` |
| Enrollment | billable term; credits within the program range; one per student and term; enrolled on or before start + 7; admit term not later | `academics.services.enroll` | 422 `TERM_NOT_BILLABLE`, 422 `CREDITS_OUT_OF_RANGE`, 409 `ENROLLMENT_EXISTS`, 400 | `tests/services/test_services_academics.py`, `tests/api/test_api_academics.py` |
| Status changes | only the transitions in each `lifecycle.py` (DATA_DICTIONARY §8) | `core.lifecycle.check_transition` | 409 `ILLEGAL_TRANSITION` | `tests/unit/test_invoice_lifecycle.py`, `tests/services/test_services_refunds.py` |
| Payment | received on or after the student's first invoice issue date; allocations dated on or after receipt; ≤ 99 allocation rows | `payments.services.record_payment`, `payments.services.allocate_payment` | 400; 409 `ALLOCATION_LIMIT_REACHED` | `tests/services/test_services_payments.py` |
| Money input | at most 2 decimal places, sent as strings | `core.money` and the serializers' money field | 400 | `tests/core/test_money.py` |
