"""Student and StudentStatusEvent (DATABASE_DESIGN.md §3, students).

``status`` is the current state; every change is recorded as a StudentStatusEvent by
``students.services`` (status "changes only via event"). Age, email domain and
active-program rules are service-level (SV) and live in ``students.services``.
"""

from django.db import models
from django.db.models import Q

from core.ids import ID_MAX_LENGTHS, id_validator
from core.models import StatusEventBase


class Student(models.Model):
    class Residency(models.TextChoices):
        DOMESTIC = "DOMESTIC"
        INTERNATIONAL = "INTERNATIONAL"

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE"
        ON_HOLD = "ON_HOLD"
        WITHDRAWN = "WITHDRAWN"
        GRADUATED = "GRADUATED"

    student_id = models.CharField(
        max_length=ID_MAX_LENGTHS["student"],
        primary_key=True,
        validators=[id_validator("student")],
    )
    first_name = models.CharField(max_length=60)
    last_name = models.CharField(max_length=60)
    email = models.EmailField(max_length=120, unique=True)
    date_of_birth = models.DateField()
    residency = models.CharField(max_length=13, choices=Residency.choices)
    program = models.ForeignKey(
        "academics.Program", on_delete=models.PROTECT, related_name="students"
    )
    admit_term = models.ForeignKey(
        "academics.Term", on_delete=models.PROTECT, related_name="admitted_students"
    )
    gpa = models.DecimalField(max_digits=3, decimal_places=2, help_text="0.00–4.00")
    need_index = models.DecimalField(
        max_digits=3, decimal_places=2, help_text="0.00–1.00, higher = more need"
    )
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.ACTIVE)
    recorded_at = models.DateTimeField(auto_now_add=True)  # execution metadata; not exported

    class Meta:
        ordering = ("student_id",)
        constraints = [
            models.CheckConstraint(
                condition=Q(gpa__gte=0) & Q(gpa__lte=4), name="student_gpa_0_to_4"
            ),
            models.CheckConstraint(
                condition=Q(need_index__gte=0) & Q(need_index__lte=1),
                name="student_need_index_0_to_1",
            ),
            models.CheckConstraint(
                condition=Q(residency__in=["DOMESTIC", "INTERNATIONAL"]),
                name="student_residency_enum",
            ),
            models.CheckConstraint(
                condition=Q(status__in=["ACTIVE", "ON_HOLD", "WITHDRAWN", "GRADUATED"]),
                name="student_status_enum",
            ),
            models.CheckConstraint(
                condition=~Q(first_name="") & ~Q(last_name="") & ~Q(email=""),
                name="student_text_not_blank",
            ),
        ]
        indexes = [
            models.Index(fields=["program", "status"], name="student_program_status_idx"),
            models.Index(fields=["status", "residency"], name="student_status_residency_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.student_id} {self.first_name} {self.last_name}"


class StudentStatusEvent(StatusEventBase):
    student = models.ForeignKey(Student, on_delete=models.PROTECT, related_name="status_events")

    class Meta(StatusEventBase.Meta):
        indexes = [
            models.Index(fields=["student", "event_date"], name="stustatusevt_student_date_idx")
        ]
