import django_filters as df

from academics.models import Enrollment, Program, Term


class ProgramFilter(df.FilterSet):
    level = df.ChoiceFilter(choices=Program.Level.choices)
    department = df.CharFilter(lookup_expr="iexact")

    class Meta:
        model = Program
        fields = ["level", "department", "is_stem", "has_lab", "is_active"]


class TermFilter(df.FilterSet):
    season = df.ChoiceFilter(choices=Term.Season.choices)

    class Meta:
        model = Term
        fields = ["year", "season", "is_billable"]


class EnrollmentFilter(df.FilterSet):
    student = df.CharFilter(field_name="student_id", help_text="Student ID")
    term = df.CharFilter(field_name="term_id", help_text="Term ID")
    program = df.CharFilter(field_name="program_id", help_text="Program ID")
    status = df.ChoiceFilter(choices=Enrollment.Status.choices)
    enrolled_from = df.DateFilter(field_name="enrolled_on", lookup_expr="gte")
    enrolled_to = df.DateFilter(field_name="enrolled_on", lookup_expr="lte")

    class Meta:
        model = Enrollment
        fields = ["student", "term", "program", "status", "enrolled_from", "enrolled_to"]
