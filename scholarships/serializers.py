"""Scholarship serializers. Money and decimals are strings (ARCHITECTURE §9.1); input is
strict (unknown fields are rejected), so a client cannot send ``evaluated_on`` (D-S7) or
any server-controlled field."""

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from academics.models import Term
from billing.models import Invoice
from core.fields import DecimalStringField, MoneyField
from core.serializers import StrictInputMixin
from scholarships.models import (
    Eligibility,
    Scholarship,
    ScholarshipAllocation,
    ScholarshipEvent,
    ScholarshipType,
)
from students.models import Student

RATIO = {"max_digits": 3, "decimal_places": 2}
POSITIVE = "Must be greater than 0."


class ScholarshipTypeSerializer(serializers.ModelSerializer):
    min_gpa = DecimalStringField(**RATIO, read_only=True, allow_null=True)
    max_gpa = DecimalStringField(**RATIO, read_only=True, allow_null=True)
    min_need_index = DecimalStringField(**RATIO, read_only=True, allow_null=True)
    award_value = MoneyField(read_only=True)
    max_amount_per_term = MoneyField(read_only=True)
    program_ids = serializers.SerializerMethodField()

    class Meta:
        model = ScholarshipType
        fields = [
            "scholarship_type_id",
            "name",
            "category",
            "exclusive_group",
            "min_gpa",
            "max_gpa",
            "min_need_index",
            "residency",
            "first_term_only",
            "program_restricted",
            "program_ids",
            "coverage_scope",
            "award_basis",
            "award_value",
            "max_amount_per_term",
            "priority",
            "effective_from",
            "effective_to",
        ]
        read_only_fields = fields

    @extend_schema_field(serializers.ListField(child=serializers.CharField()))
    def get_program_ids(self, obj):
        return sorted(tp.program_id for tp in obj.type_programs.all())


class ScholarshipTypeCreateSerializer(StrictInputMixin, serializers.Serializer):
    name = serializers.CharField(max_length=80)
    category = serializers.ChoiceField(choices=ScholarshipType.Category.choices)
    exclusive_group = serializers.CharField(
        max_length=20, required=False, allow_null=True, help_text="e.g. MERIT, FULL_RIDE"
    )
    min_gpa = DecimalStringField(**RATIO, min_value=0, max_value=4, required=False, allow_null=True)
    max_gpa = DecimalStringField(
        **RATIO, min_value=0, max_value=4, required=False, allow_null=True, help_text="exclusive"
    )
    min_need_index = DecimalStringField(
        **RATIO, min_value=0, max_value=1, required=False, allow_null=True
    )
    residency = serializers.ChoiceField(choices=ScholarshipType.Residency.choices)
    first_term_only = serializers.BooleanField()
    program_restricted = serializers.BooleanField()
    program_ids = serializers.ListField(
        child=serializers.CharField(max_length=7),
        required=False,
        default=list,
        help_text="Required (non-empty) when program_restricted is true; empty otherwise",
    )
    coverage_scope = serializers.ChoiceField(
        choices=ScholarshipType.CoverageScope.choices,
        help_text="ALL_FEE_LINES only for the single FULL_RIDE type (group FULL_RIDE, priority 0)",
    )
    award_basis = serializers.ChoiceField(choices=ScholarshipType.AwardBasis.choices)
    award_value = MoneyField(help_text="percent (0–100] or INR")
    max_amount_per_term = MoneyField()
    priority = serializers.IntegerField(min_value=0, max_value=32767)
    effective_from = serializers.DateField()
    effective_to = serializers.DateField(required=False, allow_null=True)

    def validate_award_value(self, value):
        if value <= 0:
            raise serializers.ValidationError(POSITIVE)
        return value

    def validate_max_amount_per_term(self, value):
        if value <= 0:
            raise serializers.ValidationError(POSITIVE)
        return value


class EligibilityCheckSerializer(StrictInputMixin, serializers.Serializer):
    student_id = serializers.PrimaryKeyRelatedField(
        queryset=Student.objects.all(), help_text="Student ID, e.g. STU-000001"
    )
    term_id = serializers.PrimaryKeyRelatedField(
        queryset=Term.objects.all(),
        help_text="Billable term ID; evaluated_on is always the term's invoice_date",
    )


class EligibilitySerializer(serializers.ModelSerializer):
    student = serializers.CharField(source="student_id", read_only=True)
    scholarship_type = serializers.CharField(source="scholarship_type_id", read_only=True)
    term = serializers.CharField(source="term_id", read_only=True)
    reason_codes = serializers.ListField(child=serializers.CharField(), read_only=True)
    inputs_snapshot = serializers.DictField(read_only=True)
    eligible_fee_base = MoneyField(read_only=True, allow_null=True)

    class Meta:
        model = Eligibility
        fields = [
            "eligibility_id",
            "student",
            "scholarship_type",
            "term",
            "evaluated_on",
            "is_eligible",
            "reason_codes",
            "inputs_snapshot",
            "eligible_fee_base",
        ]
        read_only_fields = fields


class AwardCreateSerializer(StrictInputMixin, serializers.Serializer):
    eligibility_id = serializers.PrimaryKeyRelatedField(
        queryset=Eligibility.objects.all(), help_text="A positive, current evaluation"
    )
    invoice_id = serializers.PrimaryKeyRelatedField(
        queryset=Invoice.objects.all(), help_text="The student's ISSUED invoice for the term"
    )
    requested_amount = MoneyField(
        required=False, help_text="Optional; may only reduce the computed award (> 0)"
    )
    award_date = serializers.DateField(
        required=False, help_text="Default: the later of evaluated_on and the invoice issue date"
    )

    def validate_requested_amount(self, value):
        if value <= 0:
            raise serializers.ValidationError(POSITIVE)
        return value


class ApplySerializer(StrictInputMixin, serializers.Serializer):
    allocated_on = serializers.DateField(
        required=False,
        help_text="Default: the award date or the invoice's latest posting/status date",
    )


class ScholarshipAllocationSerializer(serializers.ModelSerializer):
    scholarship = serializers.CharField(source="scholarship_id", read_only=True)
    invoice_line = serializers.CharField(source="invoice_line_id", read_only=True)
    amount = MoneyField(read_only=True)
    reverses = serializers.CharField(source="reverses_id", read_only=True, allow_null=True)

    class Meta:
        model = ScholarshipAllocation
        fields = [
            "allocation_id",
            "scholarship",
            "invoice_line",
            "allocation_type",
            "amount",
            "reverses",
            "reason",
            "allocated_on",
        ]
        read_only_fields = fields


class ScholarshipEventSerializer(serializers.ModelSerializer):
    scholarship = serializers.CharField(source="scholarship_id", read_only=True)

    class Meta:
        model = ScholarshipEvent
        fields = [
            "event_id",
            "scholarship",
            "from_status",
            "to_status",
            "event_date",
            "reason",
            "actor",
            "source_ref",
        ]
        read_only_fields = fields


class ScholarshipSerializer(serializers.ModelSerializer):
    student = serializers.CharField(source="student_id", read_only=True)
    scholarship_type = serializers.CharField(source="scholarship_type_id", read_only=True)
    term = serializers.CharField(source="term_id", read_only=True)
    eligibility = serializers.CharField(source="eligibility_id", read_only=True)
    invoice = serializers.CharField(source="invoice_id", read_only=True)
    awarded_amount = MoneyField(read_only=True, help_text="what was awarded (the promise)")
    applied_amount = serializers.SerializerMethodField(
        help_text="Σ net allocations actually applied to invoice lines"
    )

    class Meta:
        model = Scholarship
        fields = [
            "scholarship_id",
            "student",
            "scholarship_type",
            "term",
            "eligibility",
            "invoice",
            "awarded_amount",
            "applied_amount",
            "award_date",
            "status",
        ]
        read_only_fields = fields

    @extend_schema_field(MoneyField())
    def get_applied_amount(self, obj):
        from core.money import dsum, format_money

        return format_money(dsum(a.amount for a in obj.allocations.all()))


class ScholarshipDetailSerializer(ScholarshipSerializer):
    allocations = ScholarshipAllocationSerializer(many=True, read_only=True)
    events = serializers.SerializerMethodField()

    class Meta(ScholarshipSerializer.Meta):
        fields = [*ScholarshipSerializer.Meta.fields, "allocations", "events"]
        read_only_fields = fields

    @extend_schema_field(ScholarshipEventSerializer(many=True))
    def get_events(self, obj):
        events = sorted(obj.events.all(), key=lambda e: (e.event_date, e.event_id))
        return ScholarshipEventSerializer(events, many=True).data
