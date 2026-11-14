"""Counter gaps on the database (P6-O15): valid data has none, even after rejected operations
(their counters are released with the rollback), a reserved-but-unused trailing counter is
never a finding, and a real hole is found within its own sequence, year and parent, with the
student when the sequence belongs to one."""

import re
from datetime import date
from decimal import Decimal as D

import pytest

from academics import services as academics
from billing import services as billing
from billing.models import Invoice, LedgerEntry
from core.ids import reserve_sequence, sequence_key
from core.models import IdSequence
from payments import services as payments
from payments.models import Payment, PaymentAllocation
from reconciliation import loader, services
from reconciliation.checks.structural import _GAP_RE, GAP_SEQUENCES
from reconciliation.models import AnomalyFinding
from scholarships import services as scholarships
from scholarships.models import Scholarship

pytestmark = pytest.mark.django_db

END = date(2026, 12, 31)


def _gap_rows(run):
    return [
        (
            f.record_id,
            f.student_id,
            f.details["sequence"],
            f.details["missing_from"],
            f.details["missing_to"],
            f.details["check_id"],
        )
        for f in AnomalyFinding.objects.filter(run=run, details__problem="COUNTER_GAP").order_by(
            "finding_id"
        )
    ]


def _run(**kw):
    run = services.run_reconciliation(as_of_date=END, triggered_by="API", **{"scope": "ALL", **kw})
    assert run.status == "COMPLETED", run.summary
    return run


def _attempt(fn):
    try:
        fn()
    except Exception:  # noqa: BLE001 - each operation below is meant to be refused
        return True
    return False


def test_every_committed_counter_has_its_row_even_after_refused_operations(clean_world, terms):
    """The premise of the gap check: no service leaves a counter consumed without its row.
    Every operation below is refused after (or while) allocating an ID; afterwards each
    domain sequence's IdSequence value equals the highest counter in use."""
    s1, s17 = clean_world["S1"], clean_world["S17"]
    p17 = Payment.objects.get(student=s17)
    inv17 = Invoice.objects.get(student=s17)
    used = Scholarship.objects.filter(student=s1).first()
    refused = [
        # duplicate external_reference: the payment ID is allocated, the insert fails
        lambda: payments.record_payment(
            student=s17,
            amount=D("1.00"),
            received_date=date(2026, 9, 1),
            method="UPI",
            payer_type="SELF",
            external_reference=p17.external_reference,
            actor="t",
        ),
        # the same (student, term) twice
        lambda: academics.enroll(
            student=s17,
            term=terms["TERM-2026-FALL"],
            credits=16,
            enrolled_on=date(2026, 8, 10),
            actor="t",
        ),
        # an eligibility already used
        lambda: scholarships.award_scholarship(
            eligibility=used.eligibility, invoice=used.invoice, actor="t"
        ),
        # a second live invoice for the enrollment
        lambda: billing.generate_invoice(enrollment=inv17.enrollment, actor="t"),
        # a refund above every cap
        lambda: payments.request_refund(
            payment=p17, amount=D("999999.00"), requested_date=date(2026, 9, 2), actor="t"
        ),
        # more than the line's open amount
        lambda: payments.allocate_payment(
            p17,
            mode="MANUAL",
            lines=[(inv17.lines.get(fee_type="LAB"), D("999999.00"))],
            allocated_on=date(2026, 9, 1),
            actor="t",
        ),
    ]
    assert all(_attempt(op) for op in refused)

    ds = loader.load(as_of=END)
    highest: dict[str, int] = {}
    for table, (kind, _, scope_of) in GAP_SEQUENCES.items():
        for row in getattr(ds, table):
            m = _GAP_RE[table].match(row.pk)
            key = sequence_key(kind, **scope_of(m))
            highest[key] = max(highest.get(key, 0), int(m["n"]))
    domain = {
        k: v
        for k, v in IdSequence.objects.values_list("key", "last_value")
        if not re.match(r"(RECON|FND|GEN):", k)
    }
    assert domain == highest
    assert _gap_rows(_run()) == []


def test_a_reserved_but_unused_trailing_counter_is_not_a_finding(clean_world):
    reserve_sequence(sequence_key("payment", year=2026), 3)
    reserve_sequence(sequence_key("invoice_line", parent=Invoice.objects.first().pk), 2)
    assert _gap_rows(_run()) == []


def test_a_missing_ledger_counter_is_a_gap_in_its_year(clean_world):
    last = LedgerEntry.objects.order_by("-entry_id").first()
    m = int(last.pk.rsplit("-", 1)[1])
    moved = f"LED-2026-{m + 2:07d}"
    LedgerEntry.objects.filter(pk=last.pk).update(entry_id=moved)  # nothing refers to it
    run = _run()
    assert _gap_rows(run) == [
        (last.pk, last.student_id, "LED:2026", last.pk, f"LED-2026-{m + 1:07d}", "S-ID-GAPS-YEAR")
    ]
    f = AnomalyFinding.objects.get(run=run)
    assert (f.issue_type, f.rule_id, f.severity, f.recommended_action, f.entity_type) == (
        "INVALID_ID_FORMAT",
        "R-28",
        "LOW",
        "INVESTIGATE",
        "LedgerEntry",
    )
    assert (f.details["missing_count"], f.details["next_present_id"]) == (2, moved)


def test_a_missing_allocation_counter_is_a_gap_within_its_payment(clean_world):
    s = clean_world["S17"]
    p = Payment.objects.get(student=s)
    [a] = PaymentAllocation.objects.filter(payment=p)  # ALLOC-<p>-01, released by nothing
    PaymentAllocation.objects.filter(pk=a.pk).update(allocation_id=f"ALLOC-{p.pk}-03")
    expected = [
        (
            f"ALLOC-{p.pk}-01",
            s.pk,
            f"ALLOC:{p.pk}",
            f"ALLOC-{p.pk}-01",
            f"ALLOC-{p.pk}-02",
            "S-ID-GAPS-PARENT",
        )
    ]
    assert _gap_rows(_run()) == expected
    # the parent-scoped check also runs for that one student
    assert _gap_rows(_run(scope="STUDENT", student=s)) == expected


def test_a_student_scope_run_skips_year_sequences_and_lists_the_check(clean_world):
    last = LedgerEntry.objects.order_by("-entry_id").first()
    m = int(last.pk.rsplit("-", 1)[1])
    LedgerEntry.objects.filter(pk=last.pk).update(entry_id=f"LED-2026-{m + 2:07d}")
    run = _run(scope="STUDENT", student=clean_world["S2"])
    assert _gap_rows(run) == []  # one student's rows cannot show a year-wide hole
    assert "S-ID-GAPS-YEAR" in {x["check_id"] for x in run.summary["skipped_checks"]}
    assert len(_gap_rows(_run())) == 1  # the ALL run finds it
