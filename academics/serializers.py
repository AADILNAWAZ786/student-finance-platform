"""Academics serializers. Read serializers expose business IDs; write serializers accept them."""

from rest_framework import serializers

from academics.models import Enrollment, EnrollmentEvent, Program, Term
from core.fields import MoneyField
from core.serializers import StrictInputMixin
from students.models import Student


class ProgramSerializer(serializers.ModelSerializer):
    class Meta:
        model = Program
        fields = [
            "program_id",
            "code",
            "name",
            "level",
            "department",
            "is_stem",
            "has_lab",
            "min_credits",
            "max_credits",
            "is_active",
        ]
        read_only_fields = ["program_id"]


class ProgramCreateSerializer(StrictInputMixin, serializers.ModelSerializer):
    class Meta:
        model = Program
        fields = [
            "code",
            "name",
            "level",
            "department",
            "is_stem",
            "has_lab",
            "min_credits",
            "max_credits",
            "is_active",
        ]
        extra_kwargs = {"code": {"validators": []}}  # uniqueness: service → 400 with message


class TermSerializer(serializers.ModelSerializer):
    class Meta:
        model = Term
        fields = [
            "term_id",
            "year",
            "season",
            "is_billable",
            "invoice_date",
            "due_date",
            "start_date",
            "census_date",
            "end_date",
            "late_fee_grace_days",
        ]
        read_only_fields = ["term_id"]


class TermCreateSerializer(StrictInputMixin, serializers.ModelSerializer):
    year = serializers.IntegerField(min_value=2000, max_value=2999)

    class Meta:
        model = Term
        fields = [
            "year",
            "season",
            "is_billable",
            "invoice_date",
            "due_date",
            "start_date",
            "census_date",
            "end_date",
            "late_fee_grace_days",
        ]
        validators: list = []  # (year, season) uniqueness: service → 400 with message


class EnrollmentSerializer(serializers.ModelSerializer):
    student = serializers.CharField(source="student_id", read_only=True)
    program = serializers.CharField(source="program_id", read_only=True)
    term = serializers.CharField(source="term_id", read_only=True)

    class Meta:
        model = Enrollment
        fields = [
            "enrollment_id",
            "student",
            "program",
            "term",
            "credits",
            "status",
            "enrolled_on",
            "withdrawal_date",
            "cancellation_date",
        ]
        read_only_fields = fields


class EnrollmentCreateSerializer(StrictInputMixin, serializers.Serializer):
    student = serializers.PrimaryKeyRelatedField(
        queryset=Student.objects.all(), help_text="Student ID, e.g. STU-000001"
    )
    term = serializers.PrimaryKeyRelatedField(
        queryset=Term.objects.all(), help_text="Billable term ID, e.g. TERM-2026-FALL"
    )
    credits = serializers.IntegerField(min_value=1, max_value=24)
    enrolled_on = serializers.DateField(
        required=False, help_text="Business date; defaults to today. Must be ≤ term start + 7."
    )


class EnrollmentEventSerializer(serializers.ModelSerializer):
    enrollment = serializers.CharField(source="enrollment_id", read_only=True)

    class Meta:
        model = EnrollmentEvent
        fields = [
            "event_id",
            "enrollment",
            "from_status",
            "to_status",
            "event_date",
            "reason",
            "actor",
            "source_ref",
        ]
        read_only_fields = fields


# --- withdrawals and cancellations (Phase 5) -----------------------------------------------------


class WithdrawalRequestSerializer(StrictInputMixin, serializers.Serializer):
    withdrawal_date = serializers.DateField(
        help_text="Business date, term start ≤ date ≤ term end and ≥ enrolled_on (never the "
        "clock). Day 0 = start: days 0–7 → 80%, 8–14 → 60%, 15–27 → 40%, from 28 (census) 0%."
    )


class CancellationRequestSerializer(StrictInputMixin, serializers.Serializer):
    cancellation_date = serializers.DateField(
        help_text="Business date before the term start and ≥ enrolled_on (never the clock)"
    )


class ReleasedRowSerializer(serializers.Serializer):
    allocation_id = serializers.CharField(help_text="The new REVERSE / DEALLOCATE row")
    reverses = serializers.CharField(help_text="The allocation it releases")
    amount = MoneyField(help_text="Released amount (> 0)")


class ReleasePlanLineSerializer(serializers.Serializer):
    """BR-WDR-03 for one adjusted line: N, A, N', S, R, P, excess and the rows written."""

    adjusts_line = serializers.CharField()
    fee_type = serializers.CharField()
    adjustment_line = serializers.CharField(help_text="The new ADJUSTMENT line")
    line_net = MoneyField(help_text="N: the line's net before the adjustment")
    adjustment = MoneyField(help_text="A (< 0)")
    reduced_net = MoneyField(help_text="N' = N + A")
    scholarship_allocated = MoneyField(help_text="S")
    scholarship_reversal = MoneyField(help_text="R, reversed first (BR-SCH-07)")
    scholarship_reversals = ReleasedRowSerializer(many=True)
    payments_allocated = MoneyField(help_text="P")
    excess = MoneyField(help_text="(S − R) + P − N', deallocated newest first")
    deallocations = ReleasedRowSerializer(many=True)


class ExitAmountsSerializer(serializers.Serializer):
    original_charges = MoneyField()
    adjustments = MoneyField()
    net_charges = MoneyField()
    scholarship_applied = MoneyField()
    amount_paid = MoneyField()
    outstanding = MoneyField()


class ExitInvoiceSerializer(serializers.Serializer):
    invoice_id = serializers.CharField()
    lifecycle_status = serializers.CharField()
    collection_status = serializers.CharField()
    amounts = ExitAmountsSerializer()
    release_plan = ReleasePlanLineSerializer(many=True)


class QuotePaymentSerializer(serializers.Serializer):
    payment_id = serializers.CharField()
    payer_type = serializers.CharField()
    received_date = serializers.DateField()
    amount = MoneyField()
    net_allocated = MoneyField()
    refunds_reserved_or_processed = MoneyField()
    available = MoneyField()
    max_refundable = MoneyField()


class QuoteCalculationSerializer(serializers.Serializer):
    net_paid = MoneyField()
    ledger_balance = MoneyField()
    credit_balance = MoneyField()
    pending_refunds = MoneyField()
    credit_available = MoneyField()
    payments_available = MoneyField()


class RefundQuoteSerializer(serializers.Serializer):
    """The student's refundable summary right after the change (nothing is requested)."""

    student_id = serializers.CharField()
    max_refundable = MoneyField()
    calculation = QuoteCalculationSerializer()
    payments = QuotePaymentSerializer(many=True)


class EnrollmentExitSerializer(serializers.Serializer):
    enrollment = EnrollmentSerializer()
    kind = serializers.ChoiceField(choices=["WITHDRAWAL", "CANCELLATION"])
    effective_date = serializers.DateField()
    refund_pct = serializers.DecimalField(
        max_digits=3,
        decimal_places=2,
        allow_null=True,
        help_text="BR-WDR-02 schedule fraction for refundable lines; null for a cancellation",
    )
    financial_details_included = serializers.BooleanField(
        help_text="true for platform administrators only (decision D-W5)"
    )
    invoice = ExitInvoiceSerializer(
        required=False,
        allow_null=True,
        help_text="Platform administrators only. null when the enrollment has no non-VOID "
        "invoice (only the enrollment changed).",
    )
    refund_quote = RefundQuoteSerializer(
        required=False,
        allow_null=True,
        help_text="Platform administrators only. A quote: refunds are requested separately.",
    )
