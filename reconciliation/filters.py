import django_filters as df

from reconciliation import policy
from reconciliation.models import AnomalyFinding, BalanceSnapshot, ReconciliationRun


def _choices(values):
    return [(v, v) for v in values]


class AnomalyFindingFilter(df.FilterSet):
    run = df.CharFilter(field_name="run_id", help_text="Run ID")
    issue_type = df.ChoiceFilter(choices=_choices(policy.ISSUE_TYPES))
    student = df.CharFilter(field_name="student_id", help_text="Student ID")
    severity = df.ChoiceFilter(choices=_choices(policy.SEVERITIES))

    class Meta:
        model = AnomalyFinding
        fields = ["run", "issue_type", "student", "severity"]


class ReconciliationRunFilter(df.FilterSet):
    status = df.ChoiceFilter(choices=_choices(policy.RUN_STATUSES))
    scope = df.ChoiceFilter(choices=_choices(policy.SCOPES))
    student = df.CharFilter(field_name="student_id", help_text="Student ID")

    class Meta:
        model = ReconciliationRun
        fields = ["status", "scope", "student"]


class BalanceSnapshotFilter(df.FilterSet):
    student = df.CharFilter(field_name="student_id", help_text="Student ID")
    as_of_date = df.DateFilter()

    class Meta:
        model = BalanceSnapshot
        fields = ["student", "as_of_date"]
