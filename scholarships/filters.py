import django_filters as df

from scholarships.models import Eligibility, Scholarship, ScholarshipType


class ScholarshipTypeFilter(df.FilterSet):
    category = df.ChoiceFilter(choices=ScholarshipType.Category.choices)
    coverage_scope = df.ChoiceFilter(choices=ScholarshipType.CoverageScope.choices)
    exclusive_group = df.CharFilter()

    class Meta:
        model = ScholarshipType
        fields = ["category", "coverage_scope", "exclusive_group"]


class EligibilityFilter(df.FilterSet):
    student = df.CharFilter(field_name="student_id", help_text="Student ID")
    term = df.CharFilter(field_name="term_id", help_text="Term ID")
    scholarship_type = df.CharFilter(field_name="scholarship_type_id", help_text="Type ID")
    is_eligible = df.BooleanFilter()
    evaluated_on = df.DateFilter()

    class Meta:
        model = Eligibility
        fields = ["student", "term", "scholarship_type", "is_eligible", "evaluated_on"]


class ScholarshipFilter(df.FilterSet):
    student = df.CharFilter(field_name="student_id", help_text="Student ID")
    term = df.CharFilter(field_name="term_id", help_text="Term ID")
    scholarship_type = df.CharFilter(field_name="scholarship_type_id", help_text="Type ID")
    invoice = df.CharFilter(field_name="invoice_id", help_text="Invoice ID")
    status = df.ChoiceFilter(choices=Scholarship.Status.choices)

    class Meta:
        model = Scholarship
        fields = ["student", "term", "scholarship_type", "invoice", "status"]
