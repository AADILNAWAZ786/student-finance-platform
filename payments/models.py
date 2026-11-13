"""Payments models: Payment, PaymentAllocation, Refund, RefundEvent (DATABASE_DESIGN.md §3
payments).

DB constraints cover single-row rules only: signs, enums and required-iff rules
(ARCHITECTURE §8.3). SQLite stores decimals as REAL, so every amount rule across rows
(BR-ALC-01/02, BR-REF-01/02, Σ deallocations ≤ original) is enforced in
``payments.services`` with Python ``Decimal``.

Payment and PaymentAllocation are append-only: once inserted, ``save()`` and ``delete()``
raise ``AppendOnlyError``; a payment's allocation state and ``available`` amount are
computed, never stored. Refund changes only its ``status`` and ``processed_date``, each time
together with a RefundEvent (the lifecycle is enforced by ``payments.services``); ``save()``
refuses any change to its other fields, including ``calculation``, the request-time audit
snapshot, and writes only the two mutable columns. (``QuerySet.update()`` bypasses model
methods; no payments code uses it on these tables.)

One documented difference from the design (decision D-P1, KNOWN_LIMITATIONS P4-O1):
``Payment.method`` is Char(16), because the documented value ``SPONSOR_TRANSFER`` has 16
characters and the design's Char(14) cannot store it.
"""

import copy

from django.db import models
from django.db.models import Q

from core.ids import ID_MAX_LENGTHS, id_validator
from core.models import StatusEventBase
from payments import policy

MONEY = {"max_digits": 12, "decimal_places": 2}


class AppendOnlyError(RuntimeError):
    """An append-only payments row was updated or deleted."""


class Payment(models.Model):
    """Money received for a student, credited to the ledger in full (BR-PAY-02)."""

    class Method(models.TextChoices):
        UPI = "UPI"
        CARD = "CARD"
        BANK_TRANSFER = "BANK_TRANSFER"
        CHEQUE = "CHEQUE"
        CASH = "CASH"
        SPONSOR_TRANSFER = "SPONSOR_TRANSFER"

    class PayerType(models.TextChoices):
        SELF = "SELF"
        GUARDIAN = "GUARDIAN"
        SPONSOR = "SPONSOR"

    class Status(models.TextChoices):
        POSTED = "POSTED"

    payment_id = models.CharField(
        max_length=ID_MAX_LENGTHS["payment"],
        primary_key=True,
        validators=[id_validator("payment")],
    )
    student = models.ForeignKey(
        "students.Student", on_delete=models.PROTECT, related_name="payments"
    )
    amount = models.DecimalField(**MONEY)
    currency = models.CharField(max_length=3, default=policy.CURRENCY)
    received_date = models.DateField()
    method = models.CharField(max_length=policy.METHOD_MAX_LENGTH, choices=Method.choices)
    payer_type = models.CharField(max_length=10, choices=PayerType.choices)
    payer_name = models.CharField(max_length=80, null=True, blank=True)  # noqa: DJ001
    external_reference = models.CharField(max_length=40, unique=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=policy.POSTED)
    recorded_at = models.DateTimeField(auto_now_add=True)  # execution metadata; not exported

    class Meta:
        ordering = ("payment_id",)
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="payment_amount_gt_0"),
            models.CheckConstraint(condition=Q(currency="INR"), name="payment_currency_inr"),
            models.CheckConstraint(
                condition=Q(method__in=list(policy.METHODS)), name="payment_method_enum"
            ),
            models.CheckConstraint(
                condition=Q(payer_type__in=list(policy.PAYER_TYPES)),
                name="payment_payer_type_enum",
            ),
            models.CheckConstraint(
                condition=Q(status__in=list(policy.PAYMENT_STATUSES)), name="payment_status_enum"
            ),
        ]
        indexes = [
            models.Index(fields=["student", "received_date"], name="payment_student_date_idx")
        ]

    def __str__(self) -> str:
        return self.payment_id

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise AppendOnlyError(f"{self.payment_id}: payments are never edited")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise AppendOnlyError(f"{self.payment_id}: payments are never deleted")


class PaymentAllocation(models.Model):
    """Append-only bridge Payment ↔ InvoiceLine: ALLOCATE (> 0) or DEALLOCATE (< 0,
    referencing the ALLOCATE row it releases). Posts no ledger entry (BR-LED-01)."""

    class AllocationType(models.TextChoices):
        ALLOCATE = "ALLOCATE"
        DEALLOCATE = "DEALLOCATE"

    class Rule(models.TextChoices):
        AUTO_OLDEST_FIRST = "AUTO_OLDEST_FIRST"
        MANUAL = "MANUAL"
        WITHDRAWAL_RELEASE = "WITHDRAWAL_RELEASE"
        CANCELLATION_RELEASE = "CANCELLATION_RELEASE"
        SCHOLARSHIP_PRECEDENCE = "SCHOLARSHIP_PRECEDENCE"

    allocation_id = models.CharField(
        max_length=ID_MAX_LENGTHS["payment_allocation"],
        primary_key=True,
        validators=[id_validator("payment_allocation")],
    )
    payment = models.ForeignKey(Payment, on_delete=models.PROTECT, related_name="allocations")
    invoice_line = models.ForeignKey(
        "billing.InvoiceLine", on_delete=models.PROTECT, related_name="payment_allocations"
    )
    allocation_type = models.CharField(max_length=10, choices=AllocationType.choices)
    amount = models.DecimalField(**MONEY)
    reverses = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="reversals"
    )
    rule_applied = models.CharField(max_length=22, choices=Rule.choices)
    allocated_on = models.DateField()
    recorded_at = models.DateTimeField(auto_now_add=True)  # execution metadata; not exported

    class Meta:
        ordering = ("allocation_id",)
        constraints = [
            models.CheckConstraint(
                condition=Q(allocation_type__in=list(policy.ALLOCATION_TYPES)),
                name="payalloc_type_enum",
            ),
            models.CheckConstraint(
                condition=(Q(allocation_type="ALLOCATE") & Q(amount__gt=0))
                | (Q(allocation_type="DEALLOCATE") & Q(amount__lt=0)),
                name="payalloc_amount_sign_by_type",
            ),
            models.CheckConstraint(
                condition=(Q(allocation_type="DEALLOCATE") & Q(reverses__isnull=False))
                | (Q(allocation_type="ALLOCATE") & Q(reverses__isnull=True)),
                name="payalloc_reverses_iff_deallocate",
            ),
            models.CheckConstraint(
                condition=Q(rule_applied__in=list(policy.RULES)), name="payalloc_rule_enum"
            ),
        ]
        indexes = [
            models.Index(fields=["payment", "allocated_on"], name="payalloc_payment_date_idx"),
            models.Index(fields=["invoice_line"], name="payalloc_line_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.allocation_id} {self.allocation_type} {self.amount}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise AppendOnlyError(f"{self.allocation_id}: allocations are never edited (BR-ALC-05)")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise AppendOnlyError(f"{self.allocation_id}: allocations are never deleted (BR-ALC-05)")

    @property
    def payer_type(self) -> str:
        """The paying party (BR-PAY-03), read by ``billing.calculators.invoice_amounts``
        (decision D-P5). Not a column: it is the payment's ``payer_type``."""
        return self.payment.payer_type


class Refund(models.Model):
    """Money returned to the payer of one source payment (BR-REF-04).

    After insertion only ``status`` and ``processed_date`` may change (MUTABLE_FIELDS); every
    other column, the ``calculation`` snapshot included, is fixed (IMMUTABLE_FIELDS).
    """

    # attribute names (FKs by their *_id column)
    IMMUTABLE_FIELDS = (
        "refund_id",
        "student_id",
        "payment_id",
        "payer_type",
        "enrollment_id",
        "reason",
        "amount",
        "requested_date",
        "calculation",
        "recorded_at",
    )
    MUTABLE_FIELDS = ("status", "processed_date")

    class PayerType(models.TextChoices):
        SELF = "SELF"
        GUARDIAN = "GUARDIAN"
        SPONSOR = "SPONSOR"

    class Reason(models.TextChoices):
        WITHDRAWAL = "WITHDRAWAL"
        CANCELLATION = "CANCELLATION"
        OVERPAYMENT = "OVERPAYMENT"

    class Status(models.TextChoices):
        REQUESTED = "REQUESTED"
        APPROVED = "APPROVED"
        PROCESSED = "PROCESSED"
        REJECTED = "REJECTED"

    refund_id = models.CharField(
        max_length=ID_MAX_LENGTHS["refund"],
        primary_key=True,
        validators=[id_validator("refund")],
    )
    student = models.ForeignKey(
        "students.Student", on_delete=models.PROTECT, related_name="refunds"
    )
    payment = models.ForeignKey(Payment, on_delete=models.PROTECT, related_name="refunds")
    payer_type = models.CharField(max_length=10, choices=PayerType.choices)
    enrollment = models.ForeignKey(
        "academics.Enrollment",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="refunds",
    )
    reason = models.CharField(max_length=14, choices=Reason.choices)
    amount = models.DecimalField(**MONEY)
    requested_date = models.DateField()
    processed_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices)
    calculation = models.JSONField(
        help_text="Request-time audit snapshot (net_paid, credit_balance, payment_available, "
        "schedule_pct, …); never rewritten"
    )
    recorded_at = models.DateTimeField(auto_now_add=True)  # execution metadata; not exported

    class Meta:
        ordering = ("refund_id",)
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="refund_amount_gt_0"),
            models.CheckConstraint(
                condition=Q(payer_type__in=list(policy.PAYER_TYPES)),
                name="refund_payer_type_enum",
            ),
            models.CheckConstraint(
                condition=Q(reason__in=list(policy.REFUND_REASONS)), name="refund_reason_enum"
            ),
            models.CheckConstraint(
                condition=Q(status__in=list(policy.REFUND_STATUSES)), name="refund_status_enum"
            ),
            models.CheckConstraint(
                condition=(
                    Q(reason__in=list(policy.ENROLLMENT_REFUND_REASONS))
                    & Q(enrollment__isnull=False)
                )
                | (Q(reason=policy.OVERPAYMENT) & Q(enrollment__isnull=True)),
                name="refund_enrollment_iff_withdrawal_or_cancellation",
            ),
            models.CheckConstraint(
                # isnull=False is needed: comparisons with NULL are unknown, which a CHECK accepts
                condition=(
                    Q(status=policy.PROCESSED)
                    & Q(processed_date__isnull=False)
                    & Q(processed_date__gte=models.F("requested_date"))
                    & Q(processed_date__lte=policy.BUSINESS_DATE_MAX)
                )
                | (~Q(status=policy.PROCESSED) & Q(processed_date__isnull=True)),
                name="refund_processed_date_iff_processed",
            ),
        ]
        indexes = [
            models.Index(fields=["student", "status"], name="refund_student_status_idx"),
            models.Index(fields=["payment"], name="refund_payment_idx"),
        ]

    def __str__(self) -> str:
        return self.refund_id

    def save(self, *args, **kwargs):
        if self._state.adding:
            super().save(*args, **kwargs)
            self._original = self._immutable_values()
            return
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            extra = sorted(set(update_fields) - set(self.MUTABLE_FIELDS))
            if extra:
                raise AppendOnlyError(
                    f"{self.refund_id}: {', '.join(extra)} cannot change after the request"
                )
        original = getattr(self, "_original", None)
        if original is None:
            raise AppendOnlyError(
                f"{self.refund_id}: only a refund loaded from the database can change status"
            )
        changed = sorted(f for f, value in original.items() if self.__dict__.get(f, value) != value)
        if changed:
            raise AppendOnlyError(
                f"{original.get('refund_id', self.refund_id)}: {', '.join(changed)} cannot "
                "change after the request (only status and processed_date may)"
            )
        # write only the mutable columns, whatever the caller passed
        kwargs["update_fields"] = list(update_fields or self.MUTABLE_FIELDS)
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise AppendOnlyError(f"{self.refund_id}: refunds are never deleted")

    @classmethod
    def from_db(cls, db, field_names, values):
        instance = super().from_db(db, field_names, values)
        instance._original = instance._immutable_values()
        return instance

    def _immutable_values(self) -> dict:
        # read from __dict__ so a deferred field is never loaded (and never compared); deep
        # copy so mutating ``calculation`` in place cannot also change the snapshot
        return copy.deepcopy(
            {f: self.__dict__[f] for f in self.IMMUTABLE_FIELDS if f in self.__dict__}
        )


class RefundEvent(StatusEventBase):
    refund = models.ForeignKey(Refund, on_delete=models.PROTECT, related_name="events")

    class Meta(StatusEventBase.Meta):
        indexes = [models.Index(fields=["refund", "event_date"], name="refevt_refund_date_idx")]
