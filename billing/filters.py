import django_filters as df

from billing.models import FeeSchedule, Invoice


class FeeScheduleFilter(df.FilterSet):
    program = df.CharFilter(field_name="program_id", help_text="Program ID")
    term = df.CharFilter(field_name="term_id", help_text="Term ID")
    fee_type = df.ChoiceFilter(choices=FeeSchedule.FeeType.choices)
    residency = df.ChoiceFilter(choices=FeeSchedule.Residency.choices)
    in_force_on = df.DateFilter(
        method="filter_in_force_on",
        help_text="Only the version of each chain in force on this date (BR-FEE-03)",
    )

    class Meta:
        model = FeeSchedule
        fields = ["program", "term", "fee_type", "residency", "in_force_on"]

    def filter_in_force_on(self, queryset, name, value):
        # in force = started on or before the date, and not yet superseded on that date
        return queryset.filter(effective_from__lte=value).exclude(
            superseded_by__effective_from__lte=value
        )


class InvoiceFilter(df.FilterSet):
    student = df.CharFilter(field_name="student_id", help_text="Student ID")
    term = df.CharFilter(field_name="term_id", help_text="Term ID")
    enrollment = df.CharFilter(field_name="enrollment_id", help_text="Enrollment ID")
    lifecycle_status = df.ChoiceFilter(choices=Invoice.LifecycleStatus.choices)
    collection_status = df.ChoiceFilter(choices=Invoice.CollectionStatus.choices)

    class Meta:
        model = Invoice
        fields = ["student", "term", "enrollment", "lifecycle_status", "collection_status"]
