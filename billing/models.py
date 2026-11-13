"""Billing models: FeeSchedule, Invoice, InvoiceLine, InvoiceEvent, LedgerEntry
(DATABASE_DESIGN.md §3 billing).

DB constraints cover single-row rules only: signs, ranges, enums and required-iff rules
(ARCHITECTURE §8.3). SQLite stores decimals as REAL, so arithmetic equalities
(``gross = quantity × unit``, ``net = gross − waiver``, invoice total = Σ FEE net) are
enforced in ``billing.services`` with Python ``Decimal`` and re-checked by reconciliation.

Every row is append-only except the two derived status columns on Invoice
(``lifecycle_status`` and ``collection_status`` + ``collection_status_as_of``), which change
only together with an InvoiceEvent.

LedgerEntry was **staged** (Phase 2 decision D1): Phase 2 created the ``invoice_line``
source for CHARGE, LATE_FEE and ADJUSTMENT entries; Phase 3 added the
``scholarship_allocation`` source for SCHOLARSHIP_CREDIT / SCHOLARSHIP_REVERSAL (migration
0002); Phase 4 added the ``payment`` (PAYMENT) and ``refund`` (REFUND) sources (migration
0003). The table now matches DATABASE_DESIGN.md exactly: four source columns, seven entry
types. The scholarship and payments FKs are string references: billing never imports the
scholarships or payments apps.
``tests/models/test_billing_schema_contract.py`` checks this against the design document.
"""

from django.db import models
from django.db.models import Q

from billing import policy
from core.ids import ID_MAX_LENGTHS, id_validator
from core.models import StatusEventBase

MONEY = {"max_digits": 12, "decimal_places": 2}
LINE_ENTRY_TYPES = ["CHARGE", "LATE_FEE", "ADJUSTMENT"]
SCHOLARSHIP_ENTRY_TYPES = ["SCHOLARSHIP_CREDIT", "SCHOLARSHIP_REVERSAL"]
PAYMENT_ENTRY_TYPES = ["PAYMENT"]
REFUND_ENTRY_TYPES = ["REFUND"]
ENTRY_TYPES = [
    *LINE_ENTRY_TYPES,
    *SCHOLARSHIP_ENTRY_TYPES,
    *PAYMENT_ENTRY_TYPES,
    *REFUND_ENTRY_TYPES,
]
# the four source columns of DATABASE_DESIGN and the entry types each one posts (BR-LED-01)
LEDGER_SOURCES = {
    "invoice_line": LINE_ENTRY_TYPES,
    "scholarship_allocation": SCHOLARSHIP_ENTRY_TYPES,
    "payment": PAYMENT_ENTRY_TYPES,
    "refund": REFUND_ENTRY_TYPES,
}


def _exactly_one_source_matching_type() -> Q:
    """Exactly one of the four source FKs is set, and it is the one for entry_type."""
    branches = [
        Q(
            entry_type__in=types,
            **{f"{source}__isnull": False},
            **{f"{other}__isnull": True for other in LEDGER_SOURCES if other != source},
        )
        for source, types in LEDGER_SOURCES.items()
    ]
    return Q(*branches, _connector=Q.OR)


def _flag_matches(field: str, types) -> Q:
    """``field`` is True exactly when fee_type is one of ``types``."""
    return (Q(**{field: True}) & Q(fee_type__in=sorted(types))) | (
        Q(**{field: False}) & ~Q(fee_type__in=sorted(types))
    )


class FeeSchedule(models.Model):
    """One immutable price version (BR-FEE-03). The version chain is the history."""

    class FeeType(models.TextChoices):
        TUITION = "TUITION"
        LAB = "LAB"
        REGISTRATION = "REGISTRATION"
        TECHNOLOGY = "TECHNOLOGY"
        LIBRARY = "LIBRARY"
        STUDENT_ACTIVITY = "STUDENT_ACTIVITY"
        INTERNATIONAL_SERVICE = "INTERNATIONAL_SERVICE"

    class Residency(models.TextChoices):
        DOMESTIC = "DOMESTIC"
        INTERNATIONAL = "INTERNATIONAL"
        ALL = "ALL"

    class ChargeBasis(models.TextChoices):
        PER_CREDIT = "PER_CREDIT"
        FLAT = "FLAT"

    fee_schedule_id = models.CharField(
        max_length=ID_MAX_LENGTHS["fee_schedule"],
        primary_key=True,
        validators=[id_validator("fee_schedule")],
    )
    program = models.ForeignKey(
        "academics.Program", on_delete=models.PROTECT, related_name="fee_schedules"
    )
    term = models.ForeignKey(
        "academics.Term", on_delete=models.PROTECT, related_name="fee_schedules"
    )
    fee_type = models.CharField(max_length=24, choices=FeeType.choices)
    residency = models.CharField(max_length=13, choices=Residency.choices)
    charge_basis = models.CharField(max_length=10, choices=ChargeBasis.choices)
    amount = models.DecimalField(**MONEY, help_text="INR per credit (PER_CREDIT) or per term")
    is_scholarship_eligible = models.BooleanField()
    is_refundable = models.BooleanField()
    is_waivable = models.BooleanField()
    allocation_priority = models.PositiveSmallIntegerField()
    version = models.PositiveSmallIntegerField()
    effective_from = models.DateField()
    # "U" in DATABASE_DESIGN: a unique FK, i.e. a linear version chain
    supersedes = models.OneToOneField(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="superseded_by",
    )
    recorded_at = models.DateTimeField(auto_now_add=True)  # execution metadata; not exported

    class Meta:
        ordering = ("program_id", "term_id", "fee_type", "residency", "version")
        constraints = [
            models.UniqueConstraint(
                fields=["program", "term", "fee_type", "residency", "version"],
                name="feeschedule_unique_key_version",
            ),
            models.UniqueConstraint(
                fields=["program", "term", "fee_type", "residency", "effective_from"],
                name="feeschedule_unique_key_effective_from",
            ),
            models.CheckConstraint(condition=Q(amount__gte=0), name="feeschedule_amount_gte_0"),
            models.CheckConstraint(
                condition=Q(fee_type__in=policy.FEE_TYPES), name="feeschedule_fee_type_enum"
            ),
            models.CheckConstraint(
                condition=Q(residency__in=["DOMESTIC", "INTERNATIONAL", "ALL"]),
                name="feeschedule_residency_enum",
            ),
            models.CheckConstraint(
                condition=Q(charge_basis__in=["PER_CREDIT", "FLAT"]),
                name="feeschedule_charge_basis_enum",
            ),
            models.CheckConstraint(
                condition=~Q(fee_type="TUITION") | Q(charge_basis="PER_CREDIT"),
                name="feeschedule_tuition_per_credit",
            ),
            # D3: TUITION per residency; INTERNATIONAL_SERVICE = INTERNATIONAL; others ALL
            models.CheckConstraint(
                condition=(
                    Q(fee_type="TUITION", residency__in=["DOMESTIC", "INTERNATIONAL"])
                    | Q(fee_type="INTERNATIONAL_SERVICE", residency="INTERNATIONAL")
                    | (~Q(fee_type__in=["TUITION", "INTERNATIONAL_SERVICE"]) & Q(residency="ALL"))
                ),
                name="feeschedule_residency_mapping",
            ),
            # D5: flags and priority are fixed by fee_type
            models.CheckConstraint(
                condition=_flag_matches(
                    "is_scholarship_eligible", policy.SCHOLARSHIP_ELIGIBLE_TYPES
                ),
                name="feeschedule_scholarship_flag_by_type",
            ),
            models.CheckConstraint(
                condition=_flag_matches("is_refundable", policy.REFUNDABLE_TYPES),
                name="feeschedule_refundable_flag_by_type",
            ),
            models.CheckConstraint(
                condition=_flag_matches("is_waivable", policy.WAIVABLE_TYPES),
                name="feeschedule_waivable_flag_by_type",
            ),
            models.CheckConstraint(
                condition=Q(
                    *[
                        Q(fee_type=t, allocation_priority=p)
                        for t, p in policy.ALLOCATION_PRIORITY.items()
                    ],
                    _connector=Q.OR,
                ),
                name="feeschedule_priority_by_type",
            ),
            models.CheckConstraint(condition=Q(version__gte=1), name="feeschedule_version_gte_1"),
            models.CheckConstraint(
                condition=(Q(version=1) & Q(supersedes__isnull=True))
                | (Q(version__gt=1) & Q(supersedes__isnull=False)),
                name="feeschedule_supersedes_iff_not_v1",
            ),
        ]
        indexes = [
            models.Index(fields=["program", "term", "fee_type"], name="feesched_prog_term_type_idx")
        ]

    def __str__(self) -> str:
        return self.fee_schedule_id


class Invoice(models.Model):
    class LifecycleStatus(models.TextChoices):
        ISSUED = "ISSUED"
        CANCELLED = "CANCELLED"
        VOID = "VOID"

    class CollectionStatus(models.TextChoices):
        UNPAID = "UNPAID"
        PARTIALLY_PAID = "PARTIALLY_PAID"
        OVERDUE = "OVERDUE"
        PAID = "PAID"
        SCHOLARSHIP_COVERED = "SCHOLARSHIP_COVERED"
        ADJUSTED_TO_ZERO = "ADJUSTED_TO_ZERO"

    invoice_id = models.CharField(
        max_length=ID_MAX_LENGTHS["invoice"],
        primary_key=True,
        validators=[id_validator("invoice")],
    )
    student = models.ForeignKey(
        "students.Student", on_delete=models.PROTECT, related_name="invoices"
    )
    # deliberately no DB unique: a duplicate invoice must be representable (BR-INV-01 is SV)
    enrollment = models.ForeignKey(
        "academics.Enrollment", on_delete=models.PROTECT, related_name="invoices"
    )
    term = models.ForeignKey("academics.Term", on_delete=models.PROTECT, related_name="invoices")
    issue_date = models.DateField()
    due_date = models.DateField()
    total_amount = models.DecimalField(**MONEY, help_text="Σ FEE line net at issue; immutable")
    lifecycle_status = models.CharField(max_length=10, choices=LifecycleStatus.choices)
    collection_status = models.CharField(max_length=20, choices=CollectionStatus.choices)
    collection_status_as_of = models.DateField()
    currency = models.CharField(max_length=3, default=policy.CURRENCY)
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("invoice_id",)
        constraints = [
            models.CheckConstraint(
                condition=Q(due_date__gte=models.F("issue_date")), name="invoice_due_gte_issue"
            ),
            models.CheckConstraint(
                condition=Q(total_amount__gte=0), name="invoice_total_amount_gte_0"
            ),
            models.CheckConstraint(
                condition=Q(lifecycle_status__in=["ISSUED", "CANCELLED", "VOID"]),
                name="invoice_lifecycle_status_enum",
            ),
            models.CheckConstraint(
                condition=Q(
                    collection_status__in=[
                        "UNPAID",
                        "PARTIALLY_PAID",
                        "OVERDUE",
                        "PAID",
                        "SCHOLARSHIP_COVERED",
                        "ADJUSTED_TO_ZERO",
                    ]
                ),
                name="invoice_collection_status_enum",
            ),
            models.CheckConstraint(condition=Q(currency="INR"), name="invoice_currency_inr"),
        ]
        indexes = [
            models.Index(fields=["student", "term"], name="invoice_student_term_idx"),
            models.Index(fields=["enrollment"], name="invoice_enrollment_idx"),
            models.Index(
                fields=["lifecycle_status", "collection_status"], name="invoice_status_idx"
            ),
        ]

    def __str__(self) -> str:
        return self.invoice_id


class InvoiceLine(models.Model):
    """Append-only. FEE lines price an enrollment; LATE_FEE and ADJUSTMENT lines come later."""

    class LineType(models.TextChoices):
        FEE = "FEE"
        LATE_FEE = "LATE_FEE"
        ADJUSTMENT = "ADJUSTMENT"

    class FeeType(models.TextChoices):
        TUITION = "TUITION"
        LAB = "LAB"
        REGISTRATION = "REGISTRATION"
        TECHNOLOGY = "TECHNOLOGY"
        LIBRARY = "LIBRARY"
        STUDENT_ACTIVITY = "STUDENT_ACTIVITY"
        INTERNATIONAL_SERVICE = "INTERNATIONAL_SERVICE"
        LATE_FEE = "LATE_FEE"

    class WaiverReason(models.TextChoices):
        STAFF_DEPENDENT = "STAFF_DEPENDENT"
        HARDSHIP = "HARDSHIP"
        PG_ACTIVITY_EXEMPT = "PG_ACTIVITY_EXEMPT"
        SPONSOR_AGREEMENT = "SPONSOR_AGREEMENT"

    class AdjustmentReason(models.TextChoices):
        WITHDRAWAL = "WITHDRAWAL"
        CANCELLATION = "CANCELLATION"
        VOID = "VOID"

    line_id = models.CharField(
        max_length=ID_MAX_LENGTHS["invoice_line"],
        primary_key=True,
        validators=[id_validator("invoice_line")],
    )
    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name="lines")
    line_type = models.CharField(max_length=10, choices=LineType.choices)
    fee_schedule = models.ForeignKey(
        FeeSchedule,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="invoice_lines",
    )
    fee_type = models.CharField(max_length=24, choices=FeeType.choices)
    description = models.CharField(max_length=120)
    quantity = models.DecimalField(max_digits=6, decimal_places=2)
    unit_amount = models.DecimalField(**MONEY)
    gross_amount = models.DecimalField(**MONEY)
    waiver_amount = models.DecimalField(**MONEY, default=0)
    # NULL (not "") means "no waiver" / "not an adjustment": required-iff CHECKs below
    waiver_reason = models.CharField(  # noqa: DJ001
        max_length=24, choices=WaiverReason.choices, null=True, blank=True
    )
    net_amount = models.DecimalField(**MONEY)
    adjusts_line = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="adjustments",
    )
    adjustment_reason = models.CharField(  # noqa: DJ001
        max_length=24, choices=AdjustmentReason.choices, null=True, blank=True
    )
    is_scholarship_eligible = models.BooleanField()
    is_refundable = models.BooleanField()
    is_waivable = models.BooleanField()
    allocation_priority = models.PositiveSmallIntegerField()
    posted_date = models.DateField()
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("line_id",)
        constraints = [
            models.CheckConstraint(
                condition=Q(line_type__in=["FEE", "LATE_FEE", "ADJUSTMENT"]),
                name="invoiceline_line_type_enum",
            ),
            models.CheckConstraint(
                condition=Q(fee_type__in=[*policy.FEE_TYPES, policy.LATE_FEE]),
                name="invoiceline_fee_type_enum",
            ),
            models.CheckConstraint(
                condition=(Q(line_type="FEE") & Q(fee_schedule__isnull=False))
                | (~Q(line_type="FEE") & Q(fee_schedule__isnull=True)),
                name="invoiceline_fee_schedule_iff_fee",
            ),
            models.CheckConstraint(
                condition=(Q(line_type="LATE_FEE") & Q(fee_type="LATE_FEE"))
                | (~Q(line_type="LATE_FEE") & ~Q(fee_type="LATE_FEE"))
                | Q(line_type="ADJUSTMENT"),
                name="invoiceline_late_fee_type_iff_late_fee",
            ),
            models.CheckConstraint(condition=Q(quantity__gt=0), name="invoiceline_quantity_gt_0"),
            models.CheckConstraint(
                condition=Q(line_type="ADJUSTMENT") | Q(unit_amount__gte=0),
                name="invoiceline_unit_gte_0_for_charges",
            ),
            models.CheckConstraint(
                condition=Q(waiver_amount__gte=0), name="invoiceline_waiver_gte_0"
            ),
            models.CheckConstraint(
                condition=(Q(waiver_amount__gt=0) & Q(waiver_reason__isnull=False))
                | (Q(waiver_amount=0) & Q(waiver_reason__isnull=True)),
                name="invoiceline_waiver_reason_iff_waiver",
            ),
            models.CheckConstraint(
                condition=Q(waiver_reason__isnull=True)
                | Q(waiver_reason__in=list(policy.WAIVER_REASONS)),
                name="invoiceline_waiver_reason_enum",
            ),
            models.CheckConstraint(
                condition=Q(waiver_amount=0) | (Q(line_type="FEE") & Q(is_waivable=True)),
                name="invoiceline_waiver_only_waivable_fee",
            ),
            models.CheckConstraint(
                condition=(Q(line_type__in=["FEE", "LATE_FEE"]) & Q(net_amount__gte=0))
                | (Q(line_type="ADJUSTMENT") & Q(net_amount__lt=0)),
                name="invoiceline_net_sign_by_type",
            ),
            models.CheckConstraint(
                condition=(Q(line_type="ADJUSTMENT") & Q(adjusts_line__isnull=False))
                | (~Q(line_type="ADJUSTMENT") & Q(adjusts_line__isnull=True)),
                name="invoiceline_adjusts_line_iff_adjustment",
            ),
            models.CheckConstraint(
                # isnull=False is needed: "NULL IN (...)" is unknown, which a CHECK accepts
                condition=(
                    Q(line_type="ADJUSTMENT")
                    & Q(adjustment_reason__isnull=False)
                    & Q(adjustment_reason__in=list(policy.ADJUSTMENT_REASONS))
                )
                | (~Q(line_type="ADJUSTMENT") & Q(adjustment_reason__isnull=True)),
                name="invoiceline_adjustment_reason_iff_adjustment",
            ),
            models.CheckConstraint(
                condition=Q(line_type="FEE")
                | (
                    Q(is_scholarship_eligible=False) & Q(is_refundable=False) & Q(is_waivable=False)
                ),
                name="invoiceline_flags_false_unless_fee",
            ),
            models.CheckConstraint(
                condition=~Q(line_type="LATE_FEE")
                | Q(allocation_priority=policy.LATE_FEE_PRIORITY),
                name="invoiceline_late_fee_priority_90",
            ),
        ]
        indexes = [models.Index(fields=["invoice", "line_type"], name="invline_invoice_type_idx")]

    def __str__(self) -> str:
        return self.line_id


class InvoiceEvent(StatusEventBase):
    class Dimension(models.TextChoices):
        LIFECYCLE = "LIFECYCLE"
        COLLECTION = "COLLECTION"

    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name="events")
    dimension = models.CharField(max_length=10, choices=Dimension.choices)

    class Meta(StatusEventBase.Meta):
        constraints = [
            models.CheckConstraint(
                condition=Q(dimension__in=["LIFECYCLE", "COLLECTION"]),
                name="invoiceevent_dimension_enum",
            )
        ]
        indexes = [models.Index(fields=["invoice", "event_date"], name="invevt_invoice_date_idx")]


class LedgerEntry(models.Model):
    """Exactly one entry per source row (BR-LED-01). See the module docstring."""

    class EntryType(models.TextChoices):
        # invoice-line sources (Phase 2), scholarship-allocation sources (Phase 3),
        # payment and refund sources (Phase 4)
        CHARGE = "CHARGE"
        LATE_FEE = "LATE_FEE"
        ADJUSTMENT = "ADJUSTMENT"
        SCHOLARSHIP_CREDIT = "SCHOLARSHIP_CREDIT"
        SCHOLARSHIP_REVERSAL = "SCHOLARSHIP_REVERSAL"
        PAYMENT = "PAYMENT"
        REFUND = "REFUND"

    entry_id = models.CharField(
        max_length=ID_MAX_LENGTHS["ledger_entry"],
        primary_key=True,
        validators=[id_validator("ledger_entry")],
    )
    student = models.ForeignKey(
        "students.Student", on_delete=models.PROTECT, related_name="ledger_entries"
    )
    entry_date = models.DateField()
    entry_type = models.CharField(max_length=22, choices=EntryType.choices)
    amount = models.DecimalField(**MONEY)
    # source FKs: unique, so each source posts at most one entry (DB); completeness is REC
    invoice_line = models.OneToOneField(
        InvoiceLine,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="ledger_entry",
    )
    scholarship_allocation = models.OneToOneField(
        "scholarships.ScholarshipAllocation",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="ledger_entry",
    )
    payment = models.OneToOneField(
        "payments.Payment",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="ledger_entry",
    )
    # REFUND entries post only when the refund is PROCESSED (1:0..1, BR-REF-03)
    refund = models.OneToOneField(
        "payments.Refund",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="ledger_entry",
    )
    memo = models.CharField(max_length=120, null=True, blank=True)  # noqa: DJ001
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("entry_date", "entry_id")
        constraints = [
            models.CheckConstraint(
                condition=Q(entry_type__in=ENTRY_TYPES),
                name="ledgerentry_entry_type_enum",
            ),
            models.CheckConstraint(
                condition=(Q(entry_type="CHARGE") & Q(amount__gte=0))
                | (Q(entry_type="LATE_FEE") & Q(amount__gt=0))
                | (Q(entry_type="ADJUSTMENT") & Q(amount__lt=0))
                | (Q(entry_type="SCHOLARSHIP_CREDIT") & Q(amount__lt=0))
                | (Q(entry_type="SCHOLARSHIP_REVERSAL") & Q(amount__gt=0))
                | (Q(entry_type="PAYMENT") & Q(amount__lt=0))
                | (Q(entry_type="REFUND") & Q(amount__gt=0)),
                name="ledgerentry_amount_sign_by_type",
            ),
            # exactly one of the four source FKs, matching entry_type
            models.CheckConstraint(
                condition=_exactly_one_source_matching_type(),
                name="ledgerentry_source_matches_type",
            ),
        ]
        indexes = [models.Index(fields=["student", "entry_date"], name="ledger_student_date_idx")]

    def __str__(self) -> str:
        return f"{self.entry_id} {self.entry_type} {self.amount}"
