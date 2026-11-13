import django_filters as df

from students.models import Student


class StudentFilter(df.FilterSet):
    """Filters from ARCHITECTURE §9.2 (program, status, residency) plus admit_term."""

    program = df.CharFilter(field_name="program_id", help_text="Program ID, e.g. PRG-004")
    status = df.ChoiceFilter(choices=Student.Status.choices)
    residency = df.ChoiceFilter(choices=Student.Residency.choices)
    admit_term = df.CharFilter(field_name="admit_term_id", help_text="Term ID")

    class Meta:
        model = Student
        fields = ["program", "status", "residency", "admit_term"]
