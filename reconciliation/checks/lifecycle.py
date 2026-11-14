"""Lifecycle checks (ARCHITECTURE §12.1; DATABASE_DESIGN §6): every event chain uses only
the transitions of its app's ``lifecycle.py``; the stored status equals the last event; and
an invoice's collection status equals BR-INV-06 at its ``collection_status_as_of``.

The collection recompute mirrors ``billing.services`` exactly (decision D-R7): the services
derive the status from all of the invoice's rows (``current_amounts(invoice)``), evaluated
at ``collection_status_as_of``; so does this check, with the same calculators. An invoice
whose ``collection_status_as_of`` is after the run's as-of date is not evaluated (counted
in the summary).
"""

from __future__ import annotations

from academics.lifecycle import ENROLLMENT_TRANSITIONS
from billing import calculators as billing_calc
from billing.lifecycle import COLLECTION_STATUSES, LIFECYCLE_TRANSITIONS
from payments.lifecycle import REFUND_TRANSITIONS
from reconciliation import policy
from reconciliation.checks.base import Finding, check
from reconciliation.checks.temporal import EVENT_TABLES, event_chains, owner_student
from scholarships.lifecycle import SCHOLARSHIP_TRANSITIONS
from students.lifecycle import STUDENT_TRANSITIONS

R_LIFECYCLE = "R-48"

# collection status: ∅ → UNPAID, then only to a different status (the one BR-INV-06 derives,
# which L-COLLECTION-STATUS checks); DATABASE_DESIGN §6
COLLECTION_TRANSITIONS = {
    None: frozenset({"UNPAID"}),
    **{s: frozenset(set(COLLECTION_STATUSES) - {s}) for s in COLLECTION_STATUSES},
}

# (table, dimension) → (transition table, rule cited, stored-status attribute)
MACHINES = {
    ("student_events", None): (STUDENT_TRANSITIONS, R_LIFECYCLE, "status"),
    ("enrollment_events", None): (ENROLLMENT_TRANSITIONS, R_LIFECYCLE, "status"),
    ("scholarship_events", None): (SCHOLARSHIP_TRANSITIONS, R_LIFECYCLE, "status"),
    ("refund_events", None): (REFUND_TRANSITIONS, "BR-REF-03", "status"),
    ("invoice_events", "LIFECYCLE"): (LIFECYCLE_TRANSITIONS, "BR-INV-08", "lifecycle_status"),
    ("invoice_events", "COLLECTION"): (COLLECTION_TRANSITIONS, "BR-INV-07", "collection_status"),
}


@check("L-TRANSITIONS", family="LIFECYCLE", rule_id=R_LIFECYCLE)
def event_chains_use_allowed_transitions(ds):
    """Each event continues its chain (from = previous to) with an allowed transition."""
    out, checked = [], 0
    for name, (entity, _, _) in EVENT_TABLES.items():
        for (record_id, dimension), chain in event_chains(ds, name).items():
            machine = MACHINES.get((name, dimension))
            if machine is None:  # an unknown dimension is S-ENUM-DOMAIN's finding
                continue
            transitions, rule, _ = machine
            previous = None
            for e in chain:
                checked += 1
                problem = None
                if e.from_status != previous:
                    problem = "BROKEN_CHAIN"
                elif e.to_status not in transitions.get(e.from_status, frozenset()):
                    problem = "NOT_ALLOWED"
                if problem:
                    out.append(
                        Finding(
                            policy.ILLEGAL_TRANSITION,
                            rule,
                            entity,
                            str(e.pk),
                            owner_student(ds, name, record_id),
                            details={
                                "record": record_id,
                                "dimension": dimension,
                                "problem": problem,
                                "expected_from_status": previous,
                                "from_status": e.from_status,
                                "to_status": e.to_status,
                            },
                        )
                    )
                previous = e.to_status
    return checked, out


OWNERS = {
    "student_events": ("students", "Student"),
    "enrollment_events": ("enrollments", "Enrollment"),
    "scholarship_events": ("scholarships", "Scholarship"),
    "refund_events": ("refunds", "Refund"),
    "invoice_events": ("invoices", "Invoice"),
}


@check("L-STORED-STATUS", family="LIFECYCLE", rule_id=R_LIFECYCLE)
def stored_status_is_last_event(ds):
    """Each record's stored status equals the to_status of its last event (per dimension)."""
    out, checked = [], 0
    for (name, dimension), (_, rule, attr) in MACHINES.items():
        table, entity = OWNERS[name]
        chains = event_chains(ds, name)
        for row in getattr(ds, table):
            checked += 1
            chain = chains.get((row.pk, dimension), [])
            last = chain[-1].to_status if chain else None
            stored = getattr(row, attr)
            if stored != last:
                issue = (
                    policy.INVOICE_STATUS_MISMATCH
                    if name == "invoice_events"
                    else policy.ILLEGAL_TRANSITION
                )
                out.append(
                    Finding(
                        issue,
                        rule,
                        entity,
                        str(row.pk),
                        row.pk if table == "students" else row.student_id,
                        details={
                            "problem": "STORED_STATUS_NOT_LAST_EVENT" if chain else "NO_EVENTS",
                            "dimension": dimension,
                            "stored_status": stored,
                            "last_event_status": last,
                        },
                    )
                )
    return checked, out


@check("L-COLLECTION-STATUS", family="LIFECYCLE", rule_id="BR-INV-07")
def collection_status_is_derived(ds):
    """collection_status = BR-INV-06 at collection_status_as_of (as the services derive it)."""
    out, checked = [], 0
    for inv in ds.invoices:
        if inv.collection_status_as_of > ds.as_of:
            continue
        amounts = billing_calc.invoice_amounts(
            ds.lines_by_invoice.get(inv.pk, []),
            ds.scholarship_allocations_by_invoice.get(inv.pk, []),
            ds.payment_allocations_by_invoice.get(inv.pk, []),
        )
        try:
            derived = billing_calc.collection_status(
                amounts, due_date=inv.due_date, as_of=inv.collection_status_as_of
            )
        except ValueError:
            continue  # negative amounts: A-INVOICE-AMOUNTS reports them (BR-INV-04)
        checked += 1
        if derived != inv.collection_status:
            out.append(
                Finding(
                    policy.INVOICE_STATUS_MISMATCH,
                    "BR-INV-07",
                    "Invoice",
                    str(inv.pk),
                    inv.student_id,
                    details={
                        "stored_status": inv.collection_status,
                        "derived_status": derived,
                        "collection_status_as_of": str(inv.collection_status_as_of),
                        "outstanding": str(amounts.outstanding),
                    },
                )
            )
    return checked, out


def collection_not_evaluable(ds) -> int:
    """Invoices whose collection_status_as_of is after the run's as-of date."""
    return sum(1 for inv in ds.invoices if inv.collection_status_as_of > ds.as_of)
