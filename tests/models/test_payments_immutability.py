"""Model-level immutability of payment records (corrective Phase 4 delta).

* Payment: insert only; any save of a persisted payment raises AppendOnlyError.
* Refund: after insertion only ``status`` and ``processed_date`` may change; every other
  field (``calculation``, the request-time audit snapshot, included) is refused, and the
  database row is left untouched.
"""

from datetime import date
from decimal import Decimal as D

import pytest

from payments import services
from payments.models import AppendOnlyError, Payment, Refund

pytestmark = pytest.mark.django_db

REQ, APPROVE, PROCESS = date(2026, 8, 21), date(2026, 8, 22), date(2026, 8, 23)


def _db_row(model, pk):
    return model.objects.filter(pk=pk).values().get()


# --- Payment ----------------------------------------------------------------------------------


@pytest.mark.parametrize(("field", "value"), [("amount", D("1.00")), ("payer_type", "SPONSOR")])
def test_a_persisted_payment_cannot_be_modified(record, field, value):
    payment = record("1000.00", payer="SELF")
    before = _db_row(Payment, payment.pk)
    persisted = Payment.objects.get(pk=payment.pk)
    setattr(persisted, field, value)
    with pytest.raises(AppendOnlyError, match="never edited"):
        persisted.save()
    assert _db_row(Payment, payment.pk) == before


def test_even_an_unchanged_payment_cannot_be_saved_again(record):
    payment = record("1000.00")
    with pytest.raises(AppendOnlyError):
        payment.save()  # the instance that was just inserted
    with pytest.raises(AppendOnlyError):
        Payment.objects.get(pk=payment.pk).save(update_fields=["payer_name"])


def test_payment_inserts_still_work(invoice):
    Payment(
        payment_id="PAY-2026-000777",
        student=invoice.student,
        amount=D("10.00"),
        received_date=date(2026, 8, 20),
        method="CASH",
        payer_type="SELF",
        external_reference="IMM-1",
    ).save(force_insert=True)
    assert Payment.objects.filter(pk="PAY-2026-000777").exists()


# --- Refund ------------------------------------------------------------------------------------


@pytest.fixture
def refund(record, request_refund):
    """A REQUESTED 5,000 refund from a 174,000 payment (164,000 AUTO-allocated)."""
    payment = record("174000.00", payer="SPONSOR", auto=True)
    return request_refund(payment, "5000.00", on=REQ)


def _rejected(refund_id, mutate, *, save_kwargs=None):
    before = _db_row(Refund, refund_id)
    persisted = Refund.objects.get(pk=refund_id)
    mutate(persisted)
    with pytest.raises(AppendOnlyError, match="cannot change after the request"):
        persisted.save(**(save_kwargs or {}))
    assert _db_row(Refund, refund_id) == before  # nothing written


def test_changing_the_calculation_is_rejected(refund):
    def tamper(r):
        r.calculation = {**r.calculation, "net_paid": "999999.00"}

    _rejected(refund.pk, tamper)


def test_changing_one_key_of_the_calculation_in_place_is_rejected(refund):
    """A JSON field mutated in place is a different dict from the loaded snapshot."""
    persisted = Refund.objects.get(pk=refund.pk)
    persisted.calculation["payment_available"] = "0.00"
    with pytest.raises(AppendOnlyError, match="calculation"):
        persisted.save()


def test_changing_the_amount_is_rejected(refund):
    _rejected(refund.pk, lambda r: setattr(r, "amount", D("4000.00")))


def test_changing_the_source_payment_is_rejected(refund, record):
    other = record("100.00")
    _rejected(refund.pk, lambda r: setattr(r, "payment", other))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("payer_type", "SELF"),
        ("reason", "WITHDRAWAL"),
        ("requested_date", date(2026, 8, 25)),
        ("refund_id", "REF-2026-000999"),  # a new pk would otherwise insert a copy
    ],
)
def test_other_immutable_fields_are_rejected(refund, field, value):
    _rejected(refund.pk, lambda r: setattr(r, field, value))
    assert Refund.objects.count() == 1


def test_changing_the_student_or_enrollment_is_rejected(refund, make_student, fall_enrollment):
    other = make_student()
    _rejected(refund.pk, lambda r: setattr(r, "student", other))
    _rejected(refund.pk, lambda r: setattr(r, "enrollment", fall_enrollment))


def test_update_fields_naming_an_immutable_field_is_rejected(refund):
    _rejected(
        refund.pk,
        lambda r: setattr(r, "status", "APPROVED"),
        save_kwargs={"update_fields": ["status", "amount"]},
    )


def test_status_and_processed_date_are_the_only_mutable_fields(refund):
    assert Refund.MUTABLE_FIELDS == ("status", "processed_date")
    concrete = {f.attname for f in Refund._meta.concrete_fields}
    assert set(Refund.IMMUTABLE_FIELDS) | set(Refund.MUTABLE_FIELDS) == concrete
    persisted = Refund.objects.get(pk=refund.pk)
    persisted.status = "APPROVED"
    persisted.save()  # allowed at the model level; the lifecycle is the services' job
    assert Refund.objects.get(pk=refund.pk).status == "APPROVED"


def test_lifecycle_still_works_and_the_snapshot_never_changes(refund, request_refund):
    snapshot = dict(Refund.objects.get(pk=refund.pk).calculation)
    assert snapshot["net_paid"] == "174000.00"
    request_refund(refund.payment, "1000.00", on=REQ)  # caps change after the request
    services.approve_refund(refund, on_date=APPROVE, actor="t")
    row = _db_row(Refund, refund.pk)
    assert (row["status"], row["calculation"]) == ("APPROVED", snapshot)
    services.process_refund(refund, processed_date=PROCESS, actor="t")
    row = _db_row(Refund, refund.pk)
    assert (row["status"], row["processed_date"], row["calculation"]) == (
        "PROCESSED",
        PROCESS,
        snapshot,
    )
    assert row["amount"] == D("5000.00") and row["payer_type"] == "SPONSOR"


def test_reject_still_works(refund):
    services.reject_refund(refund, on_date=APPROVE, actor="t")
    assert Refund.objects.get(pk=refund.pk).status == "REJECTED"
