"""Scholarships models: ScholarshipType, ScholarshipTypeProgram, Eligibility, Scholarship,
ScholarshipAllocation, ScholarshipEvent (DATABASE_DESIGN.md §3 scholarships).

DB constraints cover single-row rules only (signs, ranges, enums, required-iff). The
cross-row money rules are service rules, deliberately not DB constraints, so the
``SCHOLARSHIP_EXCEEDS_ELIGIBLE_FEES`` anomaly stays representable for reconciliation:
  * per-term cap (D-S5): Σ awarded_amount per student, type and term ≤ max_amount_per_term
  * BR-SCH-04: Σ net APPLY/REVERSE allocations on an invoice ≤ its eligible fee base
  * Σ net allocations of an award ≤ its awarded_amount; line open ≥ 0 (BR-ALC-02)

Every row is append-only except ``Scholarship.status``, which changes only together with a
ScholarshipEvent. ScholarshipType rows are never updated (no update endpoint; read-only admin).
"""

from django.db import models
from django.db.models import F, Q

from core.ids import ID_MAX_LENGTHS, id_validator
from core.models import StatusEventBase
from scholarships import policy

MONEY = {"max_digits": 12, "decimal_places": 2}
RATIO = {"max_digits": 3, "decimal_places": 2}


class ScholarshipType(models.Model):
    """Criteria. Immutable; a criteria change is a new type ID."""

    class Category(models.TextChoices):
        MERIT = "MERIT"
        NEED = "NEED"
        PROGRAM = "PROGRAM"
        INTERNATIONAL = "INTERNATIONAL"
        BURSARY = "BURSARY"
        FULL_RIDE = "FULL_RIDE"

    class Residency(models.TextChoices):
        DOMESTIC = "DOMESTIC"
        INTERNATIONAL = "INTERNATIONAL"
        ALL = "ALL"

    class CoverageScope(models.TextChoices):
        ELIGIBLE_LINES_ONLY = "ELIGIBLE_LINES_ONLY"
        ALL_FEE_LINES = "ALL_FEE_LINES"

    class AwardBasis(models.TextChoices):
        PERCENT = "PERCENT"
        FIXED = "FIXED"

    scholarship_type_id = models.CharField(
        max_length=ID_MAX_LENGTHS["scholarship_type"],
        primary_key=True,
        validators=[id_validator("scholarship_type")],
    )
    name = models.CharField(max_length=80)
    # DATABASE_DESIGN says Char(12), but its own value "INTERNATIONAL" has 13 characters;
    # 13 is the minimum that stores every documented value (KNOWN_LIMITATIONS, P3-O1).
    category = models.CharField(max_length=13, choices=Category.choices)
    exclusive_group = models.CharField(max_length=20, null=True, blank=True)  # noqa: DJ001
    min_gpa = models.DecimalField(**RATIO, null=True, blank=True)
    max_gpa = models.DecimalField(**RATIO, null=True, blank=True, help_text="exclusive bound")
    min_need_index = models.DecimalField(**RATIO, null=True, blank=True)
    residency = models.CharField(max_length=13, choices=Residency.choices)
    first_term_only = models.BooleanField()
    program_restricted = models.BooleanField()
    coverage_scope = models.CharField(max_length=20, choices=CoverageScope.choices)
    award_basis = models.CharField(max_length=8, choices=AwardBasis.choices)
    award_value = models.DecimalField(**MONEY, help_text="percent 0–100 or INR")
    max_amount_per_term = models.DecimalField(**MONEY)
    priority = models.PositiveSmallIntegerField(help_text="evaluation and application order")
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)
    recorded_at = models.DateTimeField(auto_now_add=True)  # execution metadata; not exported

    class Meta:
        ordering = ("priority", "scholarship_type_id")
        constraints = [
            models.CheckConstraint(
                condition=Q(category__in=policy.CATEGORIES), name="scholtype_category_enum"
            ),
            models.CheckConstraint(
                condition=Q(residency__in=policy.RESIDENCIES), name="scholtype_residency_enum"
            ),
            models.CheckConstraint(
                condition=Q(coverage_scope__in=policy.COVERAGE_SCOPES),
                name="scholtype_coverage_scope_enum",
            ),
            models.CheckConstraint(
                condition=Q(award_basis__in=policy.AWARD_BASES), name="scholtype_award_basis_enum"
            ),
            models.CheckConstraint(
                condition=Q(award_value__gt=0), name="scholtype_award_value_gt_0"
            ),
            models.CheckConstraint(
                condition=~Q(award_basis="PERCENT") | Q(award_value__lte=100),
                name="scholtype_percent_lte_100",
            ),
            models.CheckConstraint(
                condition=Q(max_amount_per_term__gt=0), name="scholtype_max_per_term_gt_0"
            ),
            models.CheckConstraint(
                condition=Q(min_gpa__isnull=True) | (Q(min_gpa__gte=0) & Q(min_gpa__lte=4)),
                name="scholtype_min_gpa_0_to_4",
            ),
            models.CheckConstraint(
                condition=Q(max_gpa__isnull=True) | (Q(max_gpa__gte=0) & Q(max_gpa__lte=4)),
                name="scholtype_max_gpa_0_to_4",
            ),
            models.CheckConstraint(
                condition=Q(min_gpa__isnull=True)
                | Q(max_gpa__isnull=True)
                | Q(min_gpa__lt=F("max_gpa")),
                name="scholtype_min_gpa_lt_max_gpa",
            ),
            models.CheckConstraint(
                condition=Q(min_need_index__isnull=True)
                | (Q(min_need_index__gte=0) & Q(min_need_index__lte=1)),
                name="scholtype_min_need_0_to_1",
            ),
            models.CheckConstraint(
                condition=Q(effective_to__isnull=True) | Q(effective_to__gte=F("effective_from")),
                name="scholtype_effective_to_gte_from",
            ),
            models.CheckConstraint(condition=~Q(name=""), name="scholtype_name_not_blank"),
            # D-S2: ALL_FEE_LINES only for the FULL_RIDE category, group and priority 0
            models.CheckConstraint(
                condition=~Q(coverage_scope="ALL_FEE_LINES")
                | Q(
                    category="FULL_RIDE",
                    exclusive_group__isnull=False,
                    exclusive_group="FULL_RIDE",
                    priority=0,
                ),
                name="scholtype_all_fee_lines_is_full_ride",
            ),
            models.UniqueConstraint(
                fields=["coverage_scope"],
                condition=Q(coverage_scope="ALL_FEE_LINES"),
                name="scholtype_single_all_fee_lines",
            ),
        ]
        indexes = [
            models.Index(fields=["priority", "scholarship_type_id"], name="scholtype_priority_idx")
        ]

    def __str__(self) -> str:
        return f"{self.scholarship_type_id} {self.name}"


class ScholarshipTypeProgram(models.Model):
    """Bridge N:M ScholarshipType ↔ Program (program-restricted types)."""

    id = models.CharField(
        max_length=ID_MAX_LENGTHS["scholarship_type_program"],
        primary_key=True,
        validators=[id_validator("scholarship_type_program")],
    )
    scholarship_type = models.ForeignKey(
        ScholarshipType, on_delete=models.PROTECT, related_name="type_programs"
    )
    program = models.ForeignKey(
        "academics.Program", on_delete=models.PROTECT, related_name="scholarship_type_programs"
    )
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("id",)
        constraints = [
            models.UniqueConstraint(
                fields=["scholarship_type", "program"], name="scholtypeprog_unique_type_program"
            )
        ]

    def __str__(self) -> str:
        return self.id


class Eligibility(models.Model):
    """One evaluation of one student × type × term (BR-SCH-01). Every evaluation is kept."""

    eligibility_id = models.CharField(
        max_length=ID_MAX_LENGTHS["eligibility"],
        primary_key=True,
        validators=[id_validator("eligibility")],
    )
    student = models.ForeignKey(
        "students.Student", on_delete=models.PROTECT, related_name="eligibilities"
    )
    scholarship_type = models.ForeignKey(
        ScholarshipType, on_delete=models.PROTECT, related_name="eligibilities"
    )
    term = models.ForeignKey(
        "academics.Term", on_delete=models.PROTECT, related_name="eligibilities"
    )
    evaluated_on = models.DateField()
    is_eligible = models.BooleanField()
    reason_codes = models.JSONField()
    inputs_snapshot = models.JSONField()
    eligible_fee_base = models.DecimalField(**MONEY, null=True, blank=True)
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("eligibility_id",)
        verbose_name_plural = "eligibilities"
        constraints = [
            models.CheckConstraint(
                condition=Q(eligible_fee_base__isnull=True) | Q(eligible_fee_base__gte=0),
                name="eligibility_fee_base_gte_0",
            )
        ]
        indexes = [
            models.Index(
                fields=["student", "scholarship_type", "term", "evaluated_on"],
                name="elig_student_type_term_idx",
            ),
            models.Index(fields=["term", "is_eligible"], name="elig_term_eligible_idx"),
        ]

    def __str__(self) -> str:
        return self.eligibility_id


class Scholarship(models.Model):
    """An award (ARCHITECTURE D3). ``awarded_amount`` is what was promised; what was
    actually applied to invoice lines is the ScholarshipAllocation rows."""

    class Status(models.TextChoices):
        OFFERED = "OFFERED"
        ACCEPTED = "ACCEPTED"
        APPLIED = "APPLIED"
        PARTIALLY_REVERSED = "PARTIALLY_REVERSED"
        REVOKED = "REVOKED"

    scholarship_id = models.CharField(
        max_length=ID_MAX_LENGTHS["scholarship"],
        primary_key=True,
        validators=[id_validator("scholarship")],
    )
    student = models.ForeignKey(
        "students.Student", on_delete=models.PROTECT, related_name="scholarships"
    )
    scholarship_type = models.ForeignKey(
        ScholarshipType, on_delete=models.PROTECT, related_name="scholarships"
    )
    term = models.ForeignKey(
        "academics.Term", on_delete=models.PROTECT, related_name="scholarships"
    )
    eligibility = models.OneToOneField(
        Eligibility, on_delete=models.PROTECT, related_name="scholarship"
    )
    invoice = models.ForeignKey(
        "billing.Invoice", on_delete=models.PROTECT, related_name="scholarships"
    )
    awarded_amount = models.DecimalField(**MONEY)
    award_date = models.DateField()
    status = models.CharField(max_length=18, choices=Status.choices)
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("scholarship_id",)
        constraints = [
            models.CheckConstraint(
                condition=Q(awarded_amount__gt=0), name="scholarship_awarded_amount_gt_0"
            ),
            models.CheckConstraint(
                condition=Q(status__in=policy.STATUSES), name="scholarship_status_enum"
            ),
        ]
        indexes = [
            models.Index(
                fields=["student", "scholarship_type", "term"], name="sch_student_type_term_idx"
            ),
            models.Index(fields=["invoice"], name="sch_invoice_idx"),
            models.Index(fields=["scholarship_type", "term"], name="sch_type_term_idx"),
        ]

    def __str__(self) -> str:
        return self.scholarship_id


class ScholarshipAllocation(models.Model):
    """Append-only: APPLY (> 0) or REVERSE (< 0, referencing the APPLY it reverses)."""

    class AllocationType(models.TextChoices):
        APPLY = "APPLY"
        REVERSE = "REVERSE"

    class Reason(models.TextChoices):
        AWARD = "AWARD"
        WITHDRAWAL = "WITHDRAWAL"
        CANCELLATION = "CANCELLATION"

    allocation_id = models.CharField(
        max_length=ID_MAX_LENGTHS["scholarship_allocation"],
        primary_key=True,
        validators=[id_validator("scholarship_allocation")],
    )
    scholarship = models.ForeignKey(
        Scholarship, on_delete=models.PROTECT, related_name="allocations"
    )
    invoice_line = models.ForeignKey(
        "billing.InvoiceLine", on_delete=models.PROTECT, related_name="scholarship_allocations"
    )
    allocation_type = models.CharField(max_length=8, choices=AllocationType.choices)
    amount = models.DecimalField(**MONEY)
    reverses = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="reversals"
    )
    reason = models.CharField(max_length=14, choices=Reason.choices)
    allocated_on = models.DateField()
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("allocation_id",)
        constraints = [
            models.CheckConstraint(
                condition=(Q(allocation_type="APPLY") & Q(amount__gt=0))
                | (Q(allocation_type="REVERSE") & Q(amount__lt=0)),
                name="schalloc_amount_sign_by_type",
            ),
            models.CheckConstraint(
                condition=(Q(allocation_type="REVERSE") & Q(reverses__isnull=False))
                | (Q(allocation_type="APPLY") & Q(reverses__isnull=True)),
                name="schalloc_reverses_iff_reverse",
            ),
            models.CheckConstraint(
                condition=(Q(allocation_type="APPLY") & Q(reason="AWARD"))
                | (Q(allocation_type="REVERSE") & Q(reason__in=list(policy.REVERSAL_REASONS))),
                name="schalloc_reason_by_type",
            ),
        ]
        indexes = [
            models.Index(fields=["invoice_line"], name="schalloc_line_idx"),
            models.Index(fields=["scholarship", "allocated_on"], name="schalloc_sch_date_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.allocation_id} {self.allocation_type} {self.amount}"


class ScholarshipEvent(StatusEventBase):
    scholarship = models.ForeignKey(Scholarship, on_delete=models.PROTECT, related_name="events")

    class Meta(StatusEventBase.Meta):
        indexes = [models.Index(fields=["scholarship", "event_date"], name="schevt_sch_date_idx")]
