"""Replay a generator operation log through the real Phase 1–6 services (parity, ARCHITECTURE §5:
"tests/datagen/test_parity.py replays a sample of generated records through the services and
asserts identical results").

Each engine operation maps to exactly one service call, made in the same order. ``replay``
returns a mapping from engine records to the service-created model instances.
"""

from __future__ import annotations

from academics import services as academics
from billing import services as billing
from billing.models import FeeSchedule
from payments import services as payments
from reconciliation import services as reconciliation
from scholarships import services as scholarships
from students import services as students

ACTOR = "datagen"


def replay(ops) -> dict:
    made: dict = {}  # id(engine record) -> model instance

    def m(rec):  # a fresh row, as an API view would load it
        obj = made[id(rec)]
        return type(obj).objects.get(pk=obj.pk)

    def keep(rec, obj):
        made[id(rec)] = obj
        return obj

    for op, a in ops:
        if op == "create_program":
            keep(a["rec"], academics.create_program(**a["fields"]))
        elif op == "create_term":
            fields = dict(a["fields"])
            fields.pop("term_id")
            keep(a["rec"], academics.create_term(**fields))
        elif op == "create_fee_schedule":
            r = a["rec"]
            keep(
                r,
                billing.create_fee_schedule(
                    program=m(r.program),
                    term=m(r.term),
                    fee_type=r.fee_type,
                    residency=r.residency,
                    charge_basis=r.charge_basis,
                    amount=r.amount,
                    effective_from=r.effective_from,
                ),
            )
        elif op == "new_fee_schedule_version":
            r = a["rec"]
            base = FeeSchedule.objects.get(pk=a["base"].pk)
            keep(
                r,
                billing.new_fee_schedule_version(
                    base, amount=r.amount, effective_from=r.effective_from
                ),
            )
        elif op == "create_scholarship_type":
            keep(
                a["rec"],
                scholarships.create_scholarship_type(
                    **a["fields"],
                    effective_from=a["effective_from"],
                    program_ids=a["program_ids"],
                ),
            )
        elif op == "create_student":
            r = a["rec"]
            keep(
                r,
                students.create_student(
                    first_name=r.first_name,
                    last_name=r.last_name,
                    email=r.email,
                    date_of_birth=r.date_of_birth,
                    residency=r.residency,
                    program=m(r.program),
                    admit_term=m(r.admit_term),
                    gpa=r.gpa,
                    need_index=r.need_index,
                    event_date=a["event_date"],
                    actor=ACTOR,
                ),
            )
        elif op == "change_status":
            s, _ = students.change_status(
                m(a["student"]),
                to_status=a["to_status"],
                event_date=a["event_date"],
                reason=a["reason"],
                actor=ACTOR,
            )
            made[id(a["student"])] = s
        elif op == "enroll":
            keep(
                a["rec"],
                academics.enroll(
                    student=m(a["student"]),
                    term=m(a["term"]),
                    credits=a["credits"],
                    enrolled_on=a["enrolled_on"],
                    actor=ACTOR,
                ),
            )
        elif op == "complete_enrollment":
            academics.complete_enrollment(m(a["enrollment"]), on_date=a["on_date"], actor=ACTOR)
        elif op == "generate_invoice":
            keep(
                a["rec"],
                billing.generate_invoice(
                    enrollment=m(a["enrollment"]), waivers=a["waivers"], actor=ACTOR
                ),
            )
        elif op == "evaluate_eligibility":
            rows = scholarships.evaluate_eligibility(student=m(a["student"]), term=m(a["term"]))
            for rec, row in zip(a["recs"], rows, strict=True):
                keep(rec, row)
        elif op == "award_scholarship":
            keep(
                a["rec"],
                scholarships.award_scholarship(
                    eligibility=m(a["eligibility"]), invoice=m(a["invoice"]), actor=ACTOR
                ),
            )
        elif op == "apply_scholarship":
            scholarships.apply_scholarship(m(a["scholarship"]), actor=ACTOR)
        elif op == "record_payment":
            r = a["rec"]
            payment, _ = payments.record_payment(
                student=m(a["student"]),
                amount=r.amount,
                received_date=r.received_date,
                method=r.method,
                payer_type=r.payer_type,
                external_reference=r.external_reference,
                payer_name=r.payer_name,
                auto_allocate=True,
                actor=ACTOR,
            )
            keep(r, payment)
        elif op == "assess_late_fees":
            billing.assess_late_fees(as_of=a["as_of"], dry_run=False, actor=ACTOR)
        elif op == "exit_enrollment":
            enrollment = m(a["enrollment"])
            if a["kind"] == "WITHDRAWAL":
                academics.withdraw_enrollment(enrollment, withdrawal_date=a["on_date"], actor=ACTOR)
            else:
                academics.cancel_enrollment(enrollment, cancellation_date=a["on_date"], actor=ACTOR)
        elif op == "request_refund":
            keep(
                a["rec"],
                payments.request_refund(
                    payment=m(a["payment"]),
                    amount=a["amount"],
                    requested_date=a["requested_date"],
                    reason=a["reason"],
                    enrollment=m(a["enrollment"]) if a["enrollment"] is not None else None,
                    actor=ACTOR,
                ),
            )
        elif op == "advance_refund":
            refund, target, on = m(a["refund"]), a["target"], a["on_date"]
            if target == "APPROVED":
                payments.approve_refund(refund, on_date=on, actor=ACTOR)
            elif target == "PROCESSED":
                payments.process_refund(refund, processed_date=on, actor=ACTOR)
            else:
                payments.reject_refund(refund, on_date=on, actor=ACTOR)
        elif op == "refresh_collection_status":
            billing.refresh_collection_status(as_of=a["as_of"], actor=ACTOR)
        elif op == "take_snapshots":
            reconciliation.take_snapshot(as_of_date=a["as_of_date"])
        else:  # pragma: no cover - a new engine operation without a replay mapping
            raise AssertionError(f"no replay for {op}")
    return made
