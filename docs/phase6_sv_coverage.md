# Phase 6: service-layer rules re-checked by reconciliation (coverage matrix)

Resolves KNOWN_LIMITATIONS P6-O7. DATABASE_DESIGN's conventions say "Every SV rule is also REC".
This matrix maps every service-layer rule in the frozen documents to the reconciliation check
that re-checks it. For each rule it gives the check's family, the rule ID the finding cites,
the issue type (one of the 14 frozen types; none was added), and the test that proves it.

## Sources

1. **DATABASE_DESIGN §2–§4**: every row marked **SV** or **REC**, and the "Rules" / "Validation"
   column entries that a Phase 1–5 service enforces (each one confirmed in the service code).
2. **ARCHITECTURE §10**, the "Service" layer row: BR-FEE-01/03, BR-INV-01/08,
   BR-SCH-02/04/05/08, BR-ALC-01/02/06, BR-REF-01/02/03, illegal lifecycle transitions, credits
   outside program range, enrollment in a non-billable term, allocation to another student's
   line.
3. **ARCHITECTURE §6**: every business rule (BR-…).

## Conventions

- **Rule ID:** the frozen rule or requirement ID the finding cites (decision D-R8).
- **Report once:** a record already reported by a more specific finding is not reported again
  by a broader check:
  - BR-SCH-03 is silent on D-R5 awards;
  - the recorded refund caps are silent on D-R4 refunds;
  - the exit and cancellation checks are silent on duplicate invoices;
  - the snapshot totals are silent when the balance itself mismatches;
  - the invoice calendar is silent when the dates are out of order.
- **Clean data:** every check below finds nothing on the valid Phase 1–5 world
  (`tests/verification/test_verify_reconciliation_clean.py`).

**Test files**, by the short code used in the tables:

| Code | File |
|---|---|
| **U** | `tests/unit/test_reconciliation_sv_rules.py` |
| **UC** | `tests/unit/test_reconciliation_checks.py` |
| **V** | `tests/verification/test_verify_reconciliation_sv_rules.py` |
| **VI** | `tests/verification/test_verify_reconciliation_issue_types.py` |
| **VA** | `tests/verification/test_verify_reconciliation_anomalies.py` |
| **UG** | `tests/unit/test_reconciliation_counter_gaps.py` |
| **VG** | `tests/verification/test_verify_reconciliation_counter_gaps.py` |

## A. DATABASE_DESIGN service-layer (SV) and REC rules

| # | Table.field: rule | Check | Family | Rule ID | Issue type | Test |
|---|---|---|---|---|---|---|
| SV-01 | Student.date_of_birth: age 16–60 at admit term start | T-STUDENT-AGE | TEMPORAL | R-39 | DATE_ORDER | V `test_student_age` |
| SV-02 | Student.admit_term ≤ first enrollment term | T-ENROLLMENT-DATES | TEMPORAL | R-39 | DATE_ORDER | V `test_enrollment_before_the_admit_term` |
| SV-03 | Student.program active | RL-STUDENT-PROGRAM | RELATIONSHIP | R-26 | RELATIONSHIP_MISMATCH | U `test_student_in_an_inactive_program` |
| SV-04 | Term: census = start + 28 | T-TERM-CENSUS (global) | TEMPORAL | BR-WDR-02 | DATE_ORDER | V `test_census_date` |
| SV-05 | Enrollment.program = student.program | RL-ENROLLMENT-PROGRAM | RELATIONSHIP | R-26 | RELATIONSHIP_MISMATCH | U `test_enrollment_program_is_the_students` |
| SV-06 | Enrollment.term billable | RL-ENROLLMENT-RULES | RELATIONSHIP | R-26 | RELATIONSHIP_MISMATCH | V `test_enrollment_in_a_non_billable_term` |
| SV-07 | Enrollment.credits within program min/max | RL-ENROLLMENT-RULES | RELATIONSHIP | R-26 | RELATIONSHIP_MISMATCH | V `test_credits_out_of_range` |
| SV-08 | Enrollment.enrolled_on ≤ start + 7 | T-ENROLLMENT-DATES | TEMPORAL | R-39 | DATE_ORDER | V `test_enrolled_after_add_drop_also_breaks_the_invoice_date_order` |
| SV-09 | Enrollment.withdrawal_date: start ≤ date ≤ end | T-EXIT-DATES | TEMPORAL | BR-WDR-02 | DATE_ORDER | U `test_exit_dates_outside_their_window` |
| SV-10 | Enrollment.cancellation_date < start | T-EXIT-DATES | TEMPORAL | BR-WDR-01 | DATE_ORDER | U `test_exit_dates_outside_their_window` |
| SV-11 | FeeSchedule.term billable | RL-BILLABLE-TERM-MASTER (global) | RELATIONSHIP | R-26 | RELATIONSHIP_MISMATCH | V `test_fee_schedule_for_a_non_billable_term` |
| SV-12 | FeeSchedule.effective_from > predecessor's | T-FEE-CHAIN (global) | TEMPORAL | BR-FEE-03 | DATE_ORDER | U `test_fee_version_chain` |
| SV-13 | FeeSchedule.supersedes: same key, version = predecessor + 1 | T-FEE-CHAIN (global) | TEMPORAL | BR-FEE-03 | DATE_ORDER | U `test_fee_version_chain` |
| SV-14 | Invoice.student = enrollment.student; Invoice.term = enrollment.term | RL-INVOICE-ENROLLMENT | RELATIONSHIP | R-26 | RELATIONSHIP_MISMATCH | U `test_invoice_student_and_term_are_the_enrollments` |
| SV-15 | Invoice.enrollment: one non-VOID invoice per enrollment (SV + REC) | Q-DUPLICATE-INVOICE | REQUIRED | BR-INV-01 | DUPLICATE_INVOICE | VA `test_duplicate_invoice` |
| SV-16 | Invoice.issue_date (BR-INV-03), due_date | T-INVOICE-DATES, T-INVOICE-CALENDAR | TEMPORAL | BR-INV-03 | DATE_ORDER | VI `test_date_order`; V `test_invoice_due_date` |
| SV-17 | Invoice.total_amount = Σ FEE line net (SV/REC) | A-INVOICE-TOTAL | ARITHMETIC | BR-INV-02 | ARITHMETIC_MISMATCH | VI `test_arithmetic_mismatch_invoice_total` |
| SV-18 | InvoiceLine.fee_schedule: version in force for the invoice's program/term/residency | RL-LINE-SCHEDULE, A-FEE-PRICE | RELATIONSHIP, ARITHMETIC | BR-FEE-01 | RELATIONSHIP_MISMATCH, ARITHMETIC_MISMATCH | V `test_fee_type_not_applicable_also_mismatches_the_schedule_residency`; U `test_fee_line_priced_from_its_schedule` (P6-O1: "latest version" part) |
| SV-19 | InvoiceLine.gross = quantity × unit (SV/REC) | A-LINE-AMOUNTS | ARITHMETIC | BR-FEE-02 | ARITHMETIC_MISMATCH | U `test_line_arithmetic` |
| SV-20 | InvoiceLine.waiver ≤ gross; only waivable types | A-LINE-AMOUNTS, A-WAIVER-WAIVABLE | ARITHMETIC | BR-WAV-01 | ARITHMETIC_MISMATCH | U `test_line_arithmetic`, `test_waiver_on_a_line_that_is_not_waivable` |
| SV-21 | InvoiceLine.net = gross − waiver (SV/REC) | A-LINE-AMOUNTS | ARITHMETIC | BR-WAV-01 | ARITHMETIC_MISMATCH | U `test_line_arithmetic` |
| SV-22 | InvoiceLine.adjusts_line: same invoice, target FEE/LATE_FEE | RL-ADJUSTMENT-TARGET | RELATIONSHIP | BR-ALC-02 | RELATIONSHIP_MISMATCH | U `test_adjustment_targets_a_charge_on_its_own_invoice` |
| SV-23 | InvoiceLine.adjusts_line: Σ adjustments ≥ −target net | A-LINE-OPEN | ARITHMETIC | BR-ALC-02 | ARITHMETIC_MISMATCH | U `test_line_open_and_adjustment_bound` |
| SV-24 | InvoiceLine.posted_date ≥ invoice.issue_date | T-INVOICE-CALENDAR | TEMPORAL | R-39 | DATE_ORDER | U `test_a_line_posted_before_its_invoice_was_issued` |
| SV-25 | LedgerEntry.student = source row's student; entry_date = source date; 1:1 (REC) | A-LEDGER-COMPLETENESS | ARITHMETIC | BR-LED-01 | ARITHMETIC_MISMATCH | VI `test_identity_failure_is_an_arithmetic_mismatch_on_the_student` |
| SV-26 | Eligibility.term billable; is_eligible = calculator output | A-ELIGIBILITY | ARITHMETIC | BR-SCH-01 | ARITHMETIC_MISMATCH | V `test_eligibility_that_the_calculator_does_not_produce` |
| SV-27 | Scholarship.eligibility: is_eligible, same student/type/term | RL-AWARD | RELATIONSHIP | BR-SCH-02 | RELATIONSHIP_MISMATCH | U `test_award_joins_invoice_and_eligibility` |
| SV-28 | Scholarship.invoice: same student/term | RL-AWARD | RELATIONSHIP | R-26 | RELATIONSHIP_MISMATCH | U `test_award_joins_invoice_and_eligibility` |
| SV-29 | Scholarship.invoice: lifecycle ISSUED (award and application) | RL-AWARD-ON-ISSUED-INVOICE | RELATIONSHIP | BR-INV-08 | RELATIONSHIP_MISMATCH | V `test_award_on_an_invoice_already_cancelled` |
| SV-30 | Scholarship.awarded_amount: BR-SCH-03 per-term cap | A-SCHOLARSHIP-CAP | ARITHMETIC | BR-SCH-03 | ARITHMETIC_MISMATCH | V `test_award_above_the_per_term_cap` |
| SV-31 | Scholarship.awarded_amount: BR-SCH-04/08 (D-R5, unchanged) | Q-SCHOLARSHIP-EXCEEDS-FEES | REQUIRED | BR-SCH-04 / BR-SCH-08 | SCHOLARSHIP_EXCEEDS_ELIGIBLE_FEES | VA `test_scholarship_exceeds_eligible_fees`; UC D-R5 tests |
| SV-32 | Scholarship.award_date ≥ evaluated_on | T-SCHOLARSHIP-DATES | TEMPORAL | R-39 | DATE_ORDER | V `test_award_dated_before_its_evaluation` |
| SV-33 | ScholarshipAllocation.invoice_line: same invoice as the award | RL-SCHOLARSHIP-ALLOCATION | RELATIONSHIP | R-26 | RELATIONSHIP_MISMATCH | U `test_scholarship_allocation_on_another_invoice` |
| SV-34 | ScholarshipAllocation.invoice_line: FEE line coverable under the type's scope | RL-SCHOLARSHIP-COVERAGE | RELATIONSHIP | BR-SCH-06 / BR-SCH-08 | RELATIONSHIP_MISMATCH | U `test_scholarship_applied_to_a_line_its_type_cannot_cover` |
| SV-35 | ScholarshipAllocation.amount: Σ per award ≤ awarded_amount | A-AWARD-APPLIED | ARITHMETIC | BR-SCH-03 | ARITHMETIC_MISMATCH | V `test_applied_above_awarded` |
| SV-36 | ScholarshipAllocation.amount: line open ≥ 0 | A-LINE-OPEN | ARITHMETIC | BR-ALC-02 | ARITHMETIC_MISMATCH | U `test_line_open_and_adjustment_bound` |
| SV-37 | ScholarshipAllocation.reverses: Σ reversals ≥ −original, same award and line | A-RELEASE-BOUNDS, RL-SCHOLARSHIP-ALLOCATION | ARITHMETIC, RELATIONSHIP | BR-SCH-07 | ARITHMETIC_MISMATCH, RELATIONSHIP_MISMATCH | U `test_scholarship_reversals_bound_and_target` |
| SV-38 | ScholarshipAllocation.allocated_on ≥ award_date (REVERSE ≥ its APPLY) | T-SCHOLARSHIP-DATES | TEMPORAL | R-39 | DATE_ORDER | V `test_award_dated_before_its_evaluation` (same check); the clean world exercises every branch |
| SV-39 | Payment.received_date ≥ student's first invoice issue_date | T-PAYMENT-FIRST-ISSUE | TEMPORAL | R-39 | DATE_ORDER | U `test_payment_before_the_first_invoice` |
| SV-40 | PaymentAllocation.invoice_line: line student = payment student; FEE/LATE_FEE | RL-PAYMENT-ALLOCATION | RELATIONSHIP | R-26 / BR-ALC-02 | RELATIONSHIP_MISMATCH | U `test_payment_allocation_joins` |
| SV-41 | PaymentAllocation.amount: BR-ALC-01 per payment | Q-PAYMENT-OVER-ALLOCATED | REQUIRED | BR-ALC-01 | PAYMENT_OVER_ALLOCATED | VA `test_payment_over_allocated`; UC tests |
| SV-42 | PaymentAllocation.amount: BR-ALC-02 per line | A-LINE-OPEN | ARITHMETIC | BR-ALC-02 | ARITHMETIC_MISMATCH | U `test_line_open_and_adjustment_bound` |
| SV-43 | PaymentAllocation.reverses: Σ deallocations ≥ −original, same line | A-RELEASE-BOUNDS, RL-PAYMENT-ALLOCATION | ARITHMETIC, RELATIONSHIP | BR-ALC-05 | ARITHMETIC_MISMATCH, RELATIONSHIP_MISMATCH | U `test_releases_never_exceed_what_they_release`, `test_payment_allocation_joins` |
| SV-44 | PaymentAllocation.allocated_on ≥ received_date (and ≥ line posted, D-P6) | T-ALLOCATION-DATES | TEMPORAL | R-39 / BR-ALC-05 | DATE_ORDER | U `test_allocation_dated_before_its_payment_line_or_release_target` |
| SV-45 | Refund.payment: same student | RL-REFUND | RELATIONSHIP | R-26 | RELATIONSHIP_MISMATCH | U `test_refund_belongs_to_its_payments_student` |
| SV-46 | Refund.payer_type copied from the payment (BR-REF-04) | RL-REFUND | RELATIONSHIP | BR-REF-04 | RELATIONSHIP_MISMATCH | VI `test_relationship_mismatch` |
| SV-47 | Refund.enrollment: WITHDRAWN / CANCELLED for those reasons (D-W6) | RL-REFUND-ENROLLMENT | RELATIONSHIP | R-26 | RELATIONSHIP_MISMATCH | V `test_withdrawal_refund_relabelled_as_a_cancellation`; U `test_refund_reason_matches_its_enrollment` |
| SV-48 | Refund.amount: BR-REF-01 (D-R4, unchanged) | Q-REFUND-EXCEEDS-NET-PAID | REQUIRED | BR-REF-01 | REFUND_EXCEEDS_NET_PAID | VA `test_refund_exceeds_net_paid_and_its_consequence`; UC D-R4 tests |
| SV-49 | Refund.amount: BR-REF-02 (credit available, payment available) | A-REFUND-RECORDED-CAPS (+ Q-PAYMENT-OVER-ALLOCATED for the live per-payment cap, P6-O2) | ARITHMETIC | BR-REF-02 | ARITHMETIC_MISMATCH | V `test_refund_above_its_recorded_credit_cap`; U `test_refund_recorded_caps` |
| SV-50 | Refund.requested_date ≥ payment.received_date; processed_date ≥ requested_date | T-REFUND-DATES | TEMPORAL | R-39 / BR-REF-03 | DATE_ORDER | V `test_refund_requested_before_its_payment`; U `test_refund_processed_before_it_was_requested` |
| SV-51 | Invoice ID's student segment = student number (REC); every ID's format, parents and year | S-ID-DOMAIN / S-ID-MASTER | STRUCTURAL | R-28 | INVALID_ID_FORMAT | VI `test_invalid_id_format`; UC `test_invalid_id_pattern` |
| SV-52 | BalanceSnapshot.reported_balance = Σ ledger ≤ as_of (REC, BR-BAL-03) | Q-BALANCE-LEDGER-MISMATCH | REQUIRED | BR-BAL-03 | BALANCE_LEDGER_MISMATCH | VA `test_balance_ledger_mismatch` |
| SV-53 | Status changes only via events (stored = last event) | L-STORED-STATUS | LIFECYCLE | R-48 / BR-INV-07/08 / BR-REF-03 | ILLEGAL_TRANSITION, INVOICE_STATUS_MISMATCH | VI `test_illegal_transition_stored_status_without_event`, `test_invoice_status_mismatch` |
| SV-54 | Uniqueness catalogue §4 (DB-enforced keys, re-checked for imported data) | S-DUP-DOMAIN / S-DUP-MASTER | STRUCTURAL | R-39 | DUPLICATE_RECORD | UC `test_duplicate_record_contract` |
| SV-55 | Every enum column holds a documented value (DB CHECK, re-checked for imported data) | S-ENUM-DOMAIN / S-ENUM-MASTER | STRUCTURAL | R-39 | INVALID_ENUM | UC `test_invalid_enum_contract` |
| SV-56 | Every foreign key resolves (DB FK, re-checked for imported data) | S-FK-ORPHAN (global) | STRUCTURAL | R-22 | ORPHAN_FK | VI `test_orphan_fk` |
| SV-57 | StatusEventBase.event_date ≥ the previous event's date (per record and dimension) | T-EVENT-ORDER | TEMPORAL | R-39 | DATE_ORDER | U `test_event_chains_dates_and_transitions` |
| SV-58 | BalanceSnapshot.total_debits / total_credits = Σ positive / Σ negative entries ≤ as_of_date | A-SNAPSHOT-TOTALS | ARITHMETIC | BR-BAL-03 | ARITHMETIC_MISMATCH | VI `test_snapshot_totals_are_checked_when_the_balance_still_matches` |
| SV-59 | BR-BAL-02 subledger identity per student (REC of every posting and allocation rule above) | B-IDENTITY | RECONCILIATION | BR-BAL-02 | ARITHMETIC_MISMATCH | VI `test_identity_failure_is_an_arithmetic_mismatch_on_the_student` |
| SV-60 | DATABASE_DESIGN §2 IDs: counters "never reused, assigned once at insert"; "Reconciliation checks format, counter gaps and duplicates" (ARCHITECTURE §12.1 "ID counter gaps"): parent-scoped sequences: INV per student and year, LINE per invoice, SCHA per award, ALLOC per payment | S-ID-GAPS-PARENT | STRUCTURAL | R-28 | INVALID_ID_FORMAT | UG `test_lines_are_numbered_per_invoice`, `test_invoices_are_numbered_per_student_and_year`, `test_allocations_are_numbered_per_payment_and_report_the_student`, `test_a_misnumbered_row_is_reported_once_not_again_as_gaps`; VG `test_a_missing_allocation_counter_is_a_gap_within_its_payment` |
| SV-61 | Same rule: global sequences (STU, PRG, SCT) and per-year sequences (ENR by term year, ELG, SCH, PAY, REF, LED, SNAP; EVT per entity and year) | S-ID-GAPS-YEAR | STRUCTURAL | R-28 | INVALID_ID_FORMAT | UG `test_consecutive_counters_have_no_gap`, `test_a_missing_counter_in_the_middle`, `test_missing_first_counters_and_separate_runs_in_order`, `test_only_holes_below_the_highest_counter_are_reported`, `test_year_boundary_each_year_is_its_own_sequence`, `test_status_events_are_numbered_per_entity_and_year`, `test_unparseable_ids_are_left_to_the_format_check`, `test_gap_findings_are_deterministic_whatever_the_row_order`, `test_scopes_are_exactly_core_ids_counter_scopes`, `test_every_sequence_finds_a_missing_first_counter`; VG `test_every_committed_counter_has_its_row_even_after_refused_operations`, `test_a_reserved_but_unused_trailing_counter_is_not_a_finding`, `test_a_missing_ledger_counter_is_a_gap_in_its_year`, `test_a_student_scope_run_skips_year_sequences_and_lists_the_check` |

## B. ARCHITECTURE §10, service-layer row

| Rule | Covered by (section A row) |
|---|---|
| BR-FEE-01 | SV-18; P6-O1: the "latest version in force" part needs a design change |
| BR-FEE-03 | SV-12, SV-13 |
| BR-INV-01 | SV-15 |
| BR-INV-08 | SV-29; A-VOID-INVOICE (U `test_void_invoice_with_money_on_it`); RL-CANCELLED-INVOICE (U `test_cancelled_invoice_iff_cancelled_enrollment`, V `test_cancelled_invoice_without_a_cancelled_enrollment_also_breaks_its_stored_status`); L-TRANSITIONS |
| BR-SCH-02 | SV-27 |
| BR-SCH-04 | SV-31, SV-36 |
| BR-SCH-05 | S-SCHOLARSHIP-GROUP (DUPLICATE_RECORD, BR-SCH-05): V `test_second_award_in_an_exclusive_group`; U `test_full_ride_with_any_other_award_and_merit_groups` |
| BR-SCH-08 | SV-31, SV-34; S-SCHOLARSHIP-GROUP (BR-SCH-08): U `test_full_ride_with_any_other_award_and_merit_groups` |
| BR-ALC-01 | SV-41 |
| BR-ALC-02 | SV-22, SV-23, SV-36, SV-40, SV-42 |
| BR-ALC-06 | Re-checked through its result: line open ≥ 0 (SV-36), releases ≤ originals (SV-43), BR-BAL-02. The LIFO choice made at release time is not re-checkable (section D) |
| BR-REF-01 | SV-48 |
| BR-REF-02 | SV-49 |
| BR-REF-03 | L-TRANSITIONS (R-48 / BR-REF-03): U `test_event_chains_dates_and_transitions`; ledger only when PROCESSED: SV-25; SV-50 |
| Illegal lifecycle transitions | L-TRANSITIONS (every chain): U `test_event_chains_dates_and_transitions` |
| Credits outside program range | SV-07 |
| Enrollment in a non-billable term | SV-06 |
| Allocation to another student's line | SV-40 |

## C. ARCHITECTURE §6, every business rule

| Rule | Coverage |
|---|---|
| BR-FEE-01 | SV-18 (P6-O1) |
| BR-FEE-02 | SV-19; A-FEE-PRICE quantity by charge basis: U `test_fee_line_priced_from_its_schedule` |
| BR-FEE-03 | SV-12, SV-13. Immutability itself is enforced by the services and cannot be seen in stored rows |
| BR-FEE-04 | RL-FEE-APPLICABLE (RELATIONSHIP_MISMATCH, BR-FEE-04): V `test_fee_type_not_applicable_also_mismatches_the_schedule_residency` |
| BR-WAV-01 | SV-20, SV-21 |
| BR-INV-01 / 02 / 03 / 04 | SV-15 / SV-17 / SV-16 / A-INVOICE-AMOUNTS (U `test_line_open_and_adjustment_bound`) |
| BR-INV-05 | Definitional: paid vs settled is not stored; derived through BR-INV-06 |
| BR-INV-06 / 07 | L-COLLECTION-STATUS, L-STORED-STATUS: VI `test_invoice_status_mismatch` |
| BR-INV-08 | Section B |
| BR-LATE-01 | A-LATE-FEE (ARITHMETIC_MISMATCH, BR-LATE-01): once per invoice, posted date, invoice ISSUED at posting, ₹500–₹5,000, ≤ 2 % (clamped) of the charges posted by the grace cutoff. Tests: V `test_a_second_late_fee`; U `test_late_fee_bounds_and_date`, `test_late_fee_twice_and_on_an_invoice_no_longer_issued`. The exact 2 % base is not re-checkable: P6-O13 |
| BR-SCH-01 | SV-26 |
| BR-SCH-02 / 03 / 04 / 05 | SV-27 / SV-30, SV-35 / SV-31 / section B |
| BR-SCH-06 | SV-34. The priority order at application time: section D |
| BR-SCH-07 | SV-37; the reversal percentage follows the adjustment (A-EXIT-ADJUSTMENTS). The proportional split at exit time: section D |
| BR-SCH-08 | Section B |
| BR-PAY-01 | Amount > 0 is a DB CHECK; external_reference uniqueness: SV-54 |
| BR-PAY-02 | SV-25 |
| BR-PAY-03 | Definitional: every payer counts in `invoice_amounts` |
| BR-ALC-01 / 02 / 05 | SV-41 / section B / SV-43 |
| BR-ALC-03 / 04 / 06 | Section D (03, 06); 04 is definitional (the remainder is unallocated credit, re-checked by BR-BAL-02) |
| BR-WDR-01 | A-EXIT-ADJUSTMENTS (ARITHMETIC_MISMATCH, BR-WDR-01): U `test_cancellation_adjusts_everything_but_registration`; RL-CANCELLED-INVOICE; SV-10 |
| BR-WDR-02 | A-EXIT-ADJUSTMENTS (ARITHMETIC_MISMATCH, BR-WDR-02, Phase 5's own `withdrawal_adjustments`): V `test_exit_adjustments_that_do_not_match_the_schedule`; U `test_withdrawal_without_its_adjustments_and_a_correct_one`, `test_exit_adjustment_on_an_enrollment_that_never_exited`; SV-04, SV-09 |
| BR-WDR-03 | Through its results (SV-36, SV-37, SV-43, BR-BAL-02); the release plan itself: section D |
| BR-REF-01 / 02 / 03 / 04 | SV-48 / SV-49 / section B / SV-46 |
| BR-LED-01, BR-BAL-01 / 02 / 03 | SV-25; snapshot and identity checks (VA, VI, UC) |

## D. Parts that cannot be re-checked from stored data (documented, not invented)

Each of these is a decision the services take on the state at that moment, and the data does
not record that state. The final-state invariants that follow from them (line open ≥ 0,
releases ≤ originals, BR-BAL-02) are re-checked instead.

| Rule part | Why it can't be re-checked | Limitation |
|---|---|---|
| BR-FEE-01: the version used was the **latest** in force | A version may be added after issue, effective before the invoice date; nothing records which versions existed at issue | P6-O1 |
| BR-LATE-01: the 2 % base = outstanding at the end of the grace day | Exits may be dated before that day after the fee was posted (D-W8), legitimately changing the as-of outstanding later | P6-O13 |
| BR-ALC-03 AUTO order; BR-ALC-06 and BR-WDR-03 LIFO release choice; BR-SCH-06 priority order; BR-SCH-07 / BR-WDR-03 proportional split | Each depends on open amounts at that moment; rows recorded later can share or precede its dates | P6-O14 |
| ScholarshipAllocation.allocated_on ≥ the invoice's latest posting or status date at application | Depends on the invoice's history at application time | P6-O14 |
| Eligibility.eligible_fee_base | Taken from the invoice that existed at evaluation, which may since be VOID and regenerated | P6-O14 |

Counter gaps (SV-60 / SV-61, resolving P6-O15) are checked only where a gap cannot be legitimate:
every ID-allocating service is `@transaction.atomic` and `core.ids.reserve_sequence` rolls back with it, so a refused
operation releases its counter. Counters above the highest stored ID (reserved but never used: RECON, FND, GEN and any
trailing reservation) are never reported, because the detector does not read `IdSequence`. Term, FeeSchedule and
ScholarshipTypeProgram IDs are natural keys with no counter, so they have no sequence to check.

## E. The 63 registered checks

GLOBAL checks are skipped, and listed, in a STUDENT-scope run (decision D-R10).

| Check | Family | Rule ID | Scope | What it checks |
|---|---|---|---|---|
| S-DUP-DOMAIN | STRUCTURAL | R-39 | PER_STUDENT | No two student-owned rows share a PK or a DATABASE_DESIGN §4 unique key. |
| S-DUP-MASTER | STRUCTURAL | R-39 | GLOBAL | No two master-data rows share a PK or a DATABASE_DESIGN §4 unique key. |
| S-ENUM-DOMAIN | STRUCTURAL | R-39 | PER_STUDENT | Every enum column of a student-owned row holds a documented value. |
| S-ENUM-MASTER | STRUCTURAL | R-39 | GLOBAL | Every enum column of a master-data row holds a documented value. |
| S-FK-ORPHAN | STRUCTURAL | R-22 | GLOBAL | No foreign key points at a missing row (SQLite foreign_key_check). |
| S-ID-DOMAIN | STRUCTURAL | R-28 | PER_STUDENT | Student-owned IDs match their core.ids pattern, parents and business year. |
| S-ID-GAPS-PARENT | STRUCTURAL | R-28 | PER_STUDENT | No counter is missing within a student's invoices of a year, an invoice's lines, an award's allocations or a payment's allocations (DATABASE_DESIGN §2 counter scopes). |
| S-ID-GAPS-YEAR | STRUCTURAL | R-28 | GLOBAL | No counter is missing in a global sequence (STU, PRG, SCT) or a per-year one (ENR, ELG, SCH, PAY, REF, LED, SNAP, EVT per entity) (DATABASE_DESIGN §2 counter scopes). |
| S-ID-MASTER | STRUCTURAL | R-28 | GLOBAL | Master-data IDs (programs, terms, fee schedules, scholarship types) are well formed. |
| S-SCHOLARSHIP-GROUP | STRUCTURAL | BR-SCH-05 | PER_STUDENT | At most one award per exclusive_group per student and term (BR-SCH-05), and a full-ride award with no other award for the student and term (BR-SCH-08); revoked awards do not count. The later award (award_date, scholarship_id) is the duplicate. |
| RL-ADJUSTMENT-TARGET | RELATIONSHIP | BR-ALC-02 | PER_STUDENT | An ADJUSTMENT line adjusts a FEE or LATE_FEE line of the same invoice. |
| RL-AWARD | RELATIONSHIP | R-26 | PER_STUDENT | An award's invoice is its student's for its term; its eligibility is a positive row for the same student, type and term (BR-SCH-02). |
| RL-AWARD-ON-ISSUED-INVOICE | RELATIONSHIP | BR-INV-08 | PER_STUDENT | An award is made, and applied, while its invoice is ISSUED (DATABASE_DESIGN SV: "lifecycle ISSUED"): no CANCELLED / VOID lifecycle event is dated before the award date or an APPLY row's date. Same-day events are not ordered by the data, so they pass. |
| RL-BILLABLE-TERM-MASTER | RELATIONSHIP | R-26 | GLOBAL | A fee schedule is for a billable term (DATABASE_DESIGN FeeSchedule). |
| RL-CANCELLED-INVOICE | RELATIONSHIP | BR-WDR-01 | PER_STUDENT | An invoice is CANCELLED exactly when its enrollment is CANCELLED (a VOID invoice stays VOID; decision D-W7) (BR-WDR-01, BR-INV-08). |
| RL-ENROLLMENT-PROGRAM | RELATIONSHIP | R-26 | PER_STUDENT | An enrollment's program is its student's program. |
| RL-ENROLLMENT-RULES | RELATIONSHIP | R-26 | PER_STUDENT | An enrollment is in a billable term, in an active program, with credits within the program's range (ARCHITECTURE §10: non-billable term, credits outside program range). |
| RL-FEE-APPLICABLE | RELATIONSHIP | BR-FEE-04 | PER_STUDENT | A FEE line's fee type applies to the enrollment (LAB only with program.has_lab, INTERNATIONAL_SERVICE only for international students: BR-FEE-04). |
| RL-INVOICE-ENROLLMENT | RELATIONSHIP | R-26 | PER_STUDENT | An invoice's student and term are its enrollment's. |
| RL-LINE-SCHEDULE | RELATIONSHIP | R-26 | PER_STUDENT | A FEE line's fee schedule is for the invoice's program, term, residency and fee type. |
| RL-PAYMENT-ALLOCATION | RELATIONSHIP | R-26 | PER_STUDENT | A payment allocation is on a FEE/LATE_FEE line of the payment's student, and a DEALLOCATE row releases an ALLOCATE row of the same payment and line (BR-ALC-05). |
| RL-REFUND | RELATIONSHIP | R-26 | PER_STUDENT | A refund is its payment's student's, goes back to that payment's payer (BR-REF-04), and any enrollment it names is the same student's (decision D-W6). |
| RL-REFUND-ENROLLMENT | RELATIONSHIP | R-26 | PER_STUDENT | A WITHDRAWAL refund names a WITHDRAWN enrollment, a CANCELLATION refund a CANCELLED one (payments.services.REFUND_ENROLLMENT_STATUS, decision D-W6; both statuses are terminal). |
| RL-SCHOLARSHIP-ALLOCATION | RELATIONSHIP | R-26 | PER_STUDENT | A scholarship allocation is on a FEE line of its award's invoice; a REVERSE row reverses an APPLY row of the same award and line (BR-SCH-07). |
| RL-SCHOLARSHIP-COVERAGE | RELATIONSHIP | BR-SCH-06 | PER_STUDENT | An APPLY row is on a line its type may cover (scholarships.calculators.coverable_lines: eligible lines, or every FEE line for the full ride) (BR-SCH-06/08). |
| RL-STUDENT-PROGRAM | RELATIONSHIP | R-26 | PER_STUDENT | A student's program is active (students.services.create_student; programs are never updated, so this holds for life). |
| T-ALLOCATION-DATES | TEMPORAL | R-39 | PER_STUDENT | An allocation is dated on or after its payment's received_date and its line's posted_date (D-P6); a release on or after the allocation it releases. |
| T-ENROLLMENT-DATES | TEMPORAL | R-39 | PER_STUDENT | The admit term starts no later than the enrolled term (SV), and enrolled_on is no later than term start + 7 days (DATABASE_DESIGN Enrollment). |
| T-EVENT-ORDER | TEMPORAL | R-39 | PER_STUDENT | Within one record's chain, no event is dated before the previous one. |
| T-EXIT-DATES | TEMPORAL | BR-WDR-02 | PER_STUDENT | A withdrawal is within the term (start ≤ date ≤ end); a cancellation is before the start; both on or after enrolled_on (BR-WDR-01/02). |
| T-FEE-CHAIN | TEMPORAL | BR-FEE-03 | GLOBAL | Each fee version supersedes the previous version of the same program, term, fee type and residency, with version + 1 and a later effective_from (BR-FEE-03). |
| T-INVOICE-CALENDAR | TEMPORAL | BR-INV-03 | PER_STUDENT | issue_date = max(term invoice_date, enrolled_on), due_date = term due_date (BR-INV-03); every line is posted on or after the invoice's issue_date (DATABASE_DESIGN). An invoice whose dates are out of order is T-INVOICE-DATES's finding and is not reported twice. |
| T-INVOICE-DATES | TEMPORAL | BR-INV-03 | PER_STUDENT | enrolled_on ≤ issue_date ≤ due_date. |
| T-PAYMENT-FIRST-ISSUE | TEMPORAL | R-39 | PER_STUDENT | A payment is received on or after its student's first invoice issue date (D-P9). |
| T-REFUND-DATES | TEMPORAL | R-39 | PER_STUDENT | requested_date ≥ the source payment's received_date; processed_date ≥ requested_date (DATABASE_DESIGN Refund). |
| T-SCHOLARSHIP-DATES | TEMPORAL | R-39 | PER_STUDENT | award_date ≥ the eligibility's evaluated_on; an APPLY row on or after the award date; a REVERSE row on or after the row it reverses (DATABASE_DESIGN Scholarship rules). |
| T-STUDENT-AGE | TEMPORAL | R-39 | PER_STUDENT | A student is 16–60 years old on the admit term's start date (DATABASE_DESIGN SV). |
| T-TERM-CENSUS | TEMPORAL | BR-WDR-02 | GLOBAL | census_date = start_date + 28 days (DATABASE_DESIGN SV; BR-WDR-02). |
| L-COLLECTION-STATUS | LIFECYCLE | BR-INV-07 | PER_STUDENT | collection_status = BR-INV-06 at collection_status_as_of (as the services derive it). |
| L-STORED-STATUS | LIFECYCLE | R-48 | PER_STUDENT | Each record's stored status equals the to_status of its last event (per dimension). |
| L-TRANSITIONS | LIFECYCLE | R-48 | PER_STUDENT | Each event continues its chain (from = previous to) with an allowed transition. |
| A-AWARD-APPLIED | ARITHMETIC | BR-SCH-03 | PER_STUDENT | Σ net allocations of an award ≤ its awarded_amount (DATABASE_DESIGN SV). |
| A-ELIGIBILITY | ARITHMETIC | BR-SCH-01 | PER_STUDENT | Every stored eligibility equals scholarships.calculators.evaluate on its own input snapshot (BR-SCH-01: "calculator output, never random"); the snapshot is for the row's term, the student's program and admit term; the term is billable. |
| A-EXIT-ADJUSTMENTS | ARITHMETIC | BR-WDR-02 | PER_STUDENT | A withdrawn enrollment's live invoice carries exactly the BR-WDR-02 adjustments, a cancelled one exactly the BR-WDR-01 adjustments (Phase 5's own calculators, on the lines posted by the exit date), dated the exit date; no other invoice carries exit adjustments. |
| A-FEE-PRICE | ARITHMETIC | BR-FEE-01 | PER_STUDENT | A FEE line's schedule was in force by the term's invoice_date (BR-FEE-01), its unit is the schedule amount and its quantity follows the charge basis (BR-FEE-02; R-45). |
| A-INVOICE-AMOUNTS | ARITHMETIC | BR-INV-04 | PER_STUDENT | scholarship_applied, amount_paid and outstanding are never negative (BR-INV-04). |
| A-INVOICE-TOTAL | ARITHMETIC | BR-INV-02 | PER_STUDENT | invoice.total_amount = Σ net of its FEE lines (BR-INV-02). |
| A-LATE-FEE | ARITHMETIC | BR-LATE-01 | PER_STUDENT | BR-LATE-01, on what the data can re-check: at most one late fee per invoice, posted the day after due_date + grace_days, on an invoice still ISSUED then, within [₹500, ₹5,000] and no more than 2 % (clamped) of the charges posted by the end of the grace period. |
| A-LEDGER-COMPLETENESS | ARITHMETIC | BR-LED-01 | PER_STUDENT | Every source row that posts has exactly one ledger entry with the right type, amount (sign included), date and student; rows that post nothing (allocations, unprocessed refunds) have none (BR-LED-01). |
| A-LINE-AMOUNTS | ARITHMETIC | BR-WAV-01 | PER_STUDENT | gross = quantity × unit (BR-FEE-02); net = gross − waiver with 0 ≤ waiver ≤ gross (BR-WAV-01). |
| A-LINE-OPEN | ARITHMETIC | BR-ALC-02 | PER_STUDENT | Every FEE/LATE_FEE line's open amount ≥ 0, and its adjustments never exceed it (BR-ALC-02), as of the run date. |
| A-REFUND-RECORDED-CAPS | ARITHMETIC | BR-REF-02 | PER_STUDENT | A refund not REJECTED is within the three caps recorded in its request-time ``calculation`` (BR-REF-01/02: net paid, credit available, source payment available). The credit-balance cap depends on the ledger at request time, which only that audit snapshot records; the per-payment cap is also re-checked live by BR-ALC-01 (P6-O2). Refunds already reported by D-R4 (REFUND_EXCEEDS_NET_PAID) are not reported again. |
| A-RELEASE-BOUNDS | ARITHMETIC | BR-ALC-05 | PER_STUDENT | Σ deallocations of an ALLOCATE row ≤ its amount (BR-ALC-05); Σ reversals of an APPLY row ≤ its amount (BR-SCH-07). |
| A-SCHOLARSHIP-CAP | ARITHMETIC | BR-SCH-03 | PER_STUDENT | An award is at most what Phase 3's award_amount allows: PERCENT × eligible base or FIXED, capped by the type's max_amount_per_term less the same type's earlier awards to the student that term (BR-SCH-03), in (award_date, scholarship_id) order. |
| A-SNAPSHOT-TOTALS | ARITHMETIC | BR-BAL-03 | PER_STUDENT | A snapshot's total_debits / total_credits are Σ positive / Σ negative ledger entries dated on or before its as_of_date (DATABASE_DESIGN §3). |
| A-VOID-INVOICE | ARITHMETIC | BR-INV-08 | PER_STUDENT | A VOID invoice has no net scholarship and no net payment on it (BR-INV-08: void only without allocations; nothing is allocated to it afterwards). |
| A-WAIVER-WAIVABLE | ARITHMETIC | BR-WAV-01 | PER_STUDENT | A waiver (> 0) is only on a waivable line (BR-WAV-01). |
| Q-BALANCE-LEDGER-MISMATCH | REQUIRED | BR-BAL-03 | PER_STUDENT | A snapshot's reported_balance = BR-BAL-01 at its as_of_date (BR-BAL-03). |
| Q-DUPLICATE-INVOICE | REQUIRED | BR-INV-01 | PER_STUDENT | At most one invoice with lifecycle ≠ VOID per enrollment (BR-INV-01); every later one (by issue_date, invoice_id) is a duplicate. |
| Q-PAYMENT-OVER-ALLOCATED | REQUIRED | BR-ALC-01 | PER_STUDENT | Σ net allocations of a payment ≤ its amount − Σ its refunds not REJECTED (BR-ALC-01), with ``payments.calculators.payment_available``. |
| Q-REFUND-EXCEEDS-NET-PAID | REQUIRED | BR-REF-01 | PER_STUDENT | Every refund not REJECTED ≤ net paid at its request, as Phase 4 computes it (BR-REF-01). |
| Q-SCHOLARSHIP-EXCEEDS-FEES | REQUIRED | BR-SCH-04 | PER_STUDENT | No award exceeds its type's eligible fee base on its invoice, and the invoice's awards never reserve more than that base (BR-SCH-03/04/08; Phase 3 guardrail). |
| B-IDENTITY | RECONCILIATION | BR-BAL-02 | PER_STUDENT | Per student: ledger balance = Σ invoice outstanding − unallocated credit (BR-BAL-02; decision D-R3: reported as ARITHMETIC_MISMATCH on the student). |
