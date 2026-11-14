"""Reconciliation serializers. Money is a string (ARCHITECTURE §9.1); input is strict."""

from rest_framework import serializers

from core.fields import MoneyField
from core.serializers import StrictInputMixin
from reconciliation import policy
from reconciliation.models import AnomalyFinding, BalanceSnapshot, ReconciliationRun
from students.models import Student


class BalanceSnapshotSerializer(serializers.ModelSerializer):
    student = serializers.CharField(source="student_id", read_only=True)
    reported_balance = MoneyField(read_only=True, help_text="BR-BAL-01 at as_of_date when taken")
    total_debits = MoneyField(read_only=True, help_text="Σ positive entries ≤ as_of_date")
    total_credits = MoneyField(read_only=True, help_text="Σ negative entries ≤ as_of_date")

    class Meta:
        model = BalanceSnapshot
        fields = [
            "snapshot_id",
            "student",
            "as_of_date",
            "reported_balance",
            "total_debits",
            "total_credits",
        ]


class SnapshotResultSerializer(BalanceSnapshotSerializer):
    created = serializers.BooleanField(
        read_only=True,
        help_text="false: a snapshot for this student and date already existed and is "
        "returned unchanged (decision D-R6)",
    )

    class Meta(BalanceSnapshotSerializer.Meta):
        fields = [*BalanceSnapshotSerializer.Meta.fields, "created"]


class SnapshotRequestSerializer(StrictInputMixin, serializers.Serializer):
    as_of_date = serializers.DateField(help_text="2026 business date")
    student_id = serializers.PrimaryKeyRelatedField(
        queryset=Student.objects.all(),
        required=False,
        help_text="One student; omit for every student",
    )


class SnapshotResponseSerializer(serializers.Serializer):
    as_of_date = serializers.DateField()
    student_id = serializers.CharField(allow_null=True)
    created_count = serializers.IntegerField()
    existing_count = serializers.IntegerField()
    snapshots = SnapshotResultSerializer(many=True)


class RunRequestSerializer(StrictInputMixin, serializers.Serializer):
    scope = serializers.ChoiceField(choices=policy.SCOPES)
    student_id = serializers.PrimaryKeyRelatedField(
        queryset=Student.objects.all(), required=False, help_text="Required iff scope is STUDENT"
    )
    as_of_date = serializers.DateField(help_text="2026 business date")

    def validate(self, attrs):
        attrs = super().validate(attrs)
        has_student = attrs.get("student_id") is not None
        if attrs["scope"] == policy.STUDENT and not has_student:
            raise serializers.ValidationError({"student_id": ["Required when scope is STUDENT."]})
        if attrs["scope"] == policy.ALL and has_student:
            raise serializers.ValidationError(
                {"student_id": ["Must be omitted when scope is ALL."]}
            )
        return attrs


class ReconciliationRunSerializer(serializers.ModelSerializer):
    student = serializers.CharField(source="student_id", read_only=True, allow_null=True)
    findings_count = serializers.IntegerField(read_only=True)
    summary = serializers.JSONField(
        read_only=True,
        help_text="Per-check counts, skipped checks, findings by issue type and severity, "
        "students reconciled (BR-BAL-02); for a FAILED run, {error: {type, message}}",
    )

    class Meta:
        model = ReconciliationRun
        fields = [
            "run_id",
            "scope",
            "student",
            "as_of_date",
            "status",
            "triggered_by",
            "started_at",
            "finished_at",
            "findings_count",
            "summary",
        ]


class AnomalyFindingSerializer(serializers.ModelSerializer):
    run = serializers.CharField(source="run_id", read_only=True)
    student = serializers.CharField(source="student_id", read_only=True, allow_null=True)
    expected_value = MoneyField(read_only=True, allow_null=True)
    actual_value = MoneyField(read_only=True, allow_null=True)
    details = serializers.JSONField(read_only=True, help_text="check_id, family, calculation")

    class Meta:
        model = AnomalyFinding
        fields = [
            "finding_id",
            "run",
            "issue_type",
            "rule_id",
            "severity",
            "entity_type",
            "record_id",
            "student",
            "expected_value",
            "actual_value",
            "details",
            "recommended_action",
        ]
