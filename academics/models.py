"""Academics models: Program, Term, Enrollment, EnrollmentEvent (DATABASE_DESIGN.md §3).

DB constraints cover single-row rules only (signs, ranges, enums, required-iff), per
ARCHITECTURE §8.3. Cross-row and date-relationship rules (census = start + 28,
credits within the program range, billable term, …) live in ``academics.services``.
"""

from django.db import models
from django.db.models import F, Q

from core.ids import ID_MAX_LENGTHS, id_validator
from core.models import StatusEventBase


class Program(models.Model):
    class Level(models.TextChoices):
        UG = "UG", "Undergraduate"
        PG = "PG", "Postgraduate"
        DIPLOMA = "DIPLOMA", "Diploma"

    program_id = models.CharField(
        max_length=ID_MAX_LENGTHS["program"],
        primary_key=True,
        validators=[id_validator("program")],
    )
    code = models.CharField(max_length=12, unique=True)
    name = models.CharField(max_length=120)
    level = models.CharField(max_length=8, choices=Level.choices)
    department = models.CharField(max_length=60)
    is_stem = models.BooleanField()
    has_lab = models.BooleanField()
    min_credits = models.PositiveSmallIntegerField(help_text="Credits per term")
    max_credits = models.PositiveSmallIntegerField(help_text="Credits per term")
    is_active = models.BooleanField(default=True)
    recorded_at = models.DateTimeField(auto_now_add=True)  # execution metadata; not exported

    class Meta:
        ordering = ("program_id",)
        constraints = [
            models.CheckConstraint(
                condition=Q(min_credits__gte=6)
                & Q(max_credits__lte=24)
                & Q(min_credits__lte=F("max_credits")),
                name="program_credits_6_le_min_le_max_le_24",
            ),
            models.CheckConstraint(
                condition=Q(level__in=["UG", "PG", "DIPLOMA"]), name="program_level_enum"
            ),
            models.CheckConstraint(
                condition=~Q(code="") & ~Q(name="") & ~Q(department=""),
                name="program_text_not_blank",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.program_id} {self.code}"


class Term(models.Model):
    class Season(models.TextChoices):
        SPRING = "SPRING"
        SUMMER = "SUMMER"
        FALL = "FALL"

    term_id = models.CharField(
        max_length=ID_MAX_LENGTHS["term"],
        primary_key=True,
        validators=[id_validator("term")],
    )
    year = models.PositiveSmallIntegerField()
    season = models.CharField(max_length=6, choices=Season.choices)
    is_billable = models.BooleanField()
    invoice_date = models.DateField()
    due_date = models.DateField()
    start_date = models.DateField()
    census_date = models.DateField()
    end_date = models.DateField()
    late_fee_grace_days = models.PositiveSmallIntegerField(default=14)
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("start_date", "term_id")
        constraints = [
            models.UniqueConstraint(fields=["year", "season"], name="term_unique_year_season"),
            models.CheckConstraint(
                condition=Q(invoice_date__lte=F("due_date")), name="term_invoice_le_due"
            ),
            models.CheckConstraint(
                condition=Q(start_date__lt=F("census_date")) & Q(census_date__lt=F("end_date")),
                name="term_start_lt_census_lt_end",
            ),
            models.CheckConstraint(
                condition=Q(season__in=["SPRING", "SUMMER", "FALL"]), name="term_season_enum"
            ),
        ]

    def __str__(self) -> str:
        return self.term_id


class Enrollment(models.Model):
    class Status(models.TextChoices):
        ENROLLED = "ENROLLED"
        WITHDRAWN = "WITHDRAWN"
        CANCELLED = "CANCELLED"
        COMPLETED = "COMPLETED"

    enrollment_id = models.CharField(
        max_length=ID_MAX_LENGTHS["enrollment"],
        primary_key=True,
        validators=[id_validator("enrollment")],
    )
    student = models.ForeignKey(
        "students.Student", on_delete=models.PROTECT, related_name="enrollments"
    )
    program = models.ForeignKey(Program, on_delete=models.PROTECT, related_name="enrollments")
    term = models.ForeignKey(Term, on_delete=models.PROTECT, related_name="enrollments")
    credits = models.PositiveSmallIntegerField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ENROLLED)
    enrolled_on = models.DateField()
    withdrawal_date = models.DateField(null=True, blank=True)
    cancellation_date = models.DateField(null=True, blank=True)
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("enrollment_id",)
        constraints = [
            models.UniqueConstraint(
                fields=["student", "term"], name="enrollment_unique_student_term"
            ),
            models.CheckConstraint(
                condition=Q(status__in=["ENROLLED", "WITHDRAWN", "CANCELLED", "COMPLETED"]),
                name="enrollment_status_enum",
            ),
            models.CheckConstraint(condition=Q(credits__gte=1), name="enrollment_credits_positive"),
            models.CheckConstraint(
                condition=(Q(status="WITHDRAWN") & Q(withdrawal_date__isnull=False))
                | (~Q(status="WITHDRAWN") & Q(withdrawal_date__isnull=True)),
                name="enrollment_withdrawal_date_iff_withdrawn",
            ),
            models.CheckConstraint(
                condition=(Q(status="CANCELLED") & Q(cancellation_date__isnull=False))
                | (~Q(status="CANCELLED") & Q(cancellation_date__isnull=True)),
                name="enrollment_cancellation_date_iff_cancelled",
            ),
        ]
        indexes = [
            models.Index(fields=["term", "status"], name="enrollment_term_status_idx"),
            models.Index(fields=["program", "status"], name="enrollment_program_status_idx"),
        ]

    def __str__(self) -> str:
        return self.enrollment_id


class EnrollmentEvent(StatusEventBase):
    enrollment = models.ForeignKey(Enrollment, on_delete=models.PROTECT, related_name="events")

    class Meta(StatusEventBase.Meta):
        indexes = [
            models.Index(fields=["enrollment", "event_date"], name="enrevt_enrollment_date_idx")
        ]
