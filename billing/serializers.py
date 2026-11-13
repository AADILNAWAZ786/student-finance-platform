"""Billing serializers. Money is always a string with 2 decimal places (ARCHITECTURE §9.1).

Read serializers expose business IDs. Write serializers accept only the documented
fields: fee-schedule flags, allocation priority, version and supersedes are set by the
server (decision D5), so a request that sends them is rejected with 400.
"""

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from academics.models import Enrollment, Program, Term
from billing import calculators as calc
from billing import policy
from billing.models import FeeSchedule, Invoice, InvoiceEvent, InvoiceLine, LedgerEntry
from core.fields import DecimalStringField, MoneyField
from core.money import format_money
from core.serializers import StrictInputMixin

SERVER_FIELD_MESSAGE = "Set by the server from fee_type; clients cannot send it."
SERVER_FIELDS = {
    "fee_schedule_id",
    "is_scholarship_eligible",
    "is_refundable",
    "is_waivable",
    "allocation_priority",
    "version",
    "supersedes",
    "superseded_by",
    "effective_to",
}


class _ServerFieldsMixin(StrictInputMixin):
    """Like StrictInputMixin, with a clearer message for the server-controlled fields."""

    def validate(self, attrs):
        extra = set(getattr(self, "initial_data", {}) or {}) & SERVER_FIELDS
        if extra:
            raise serializers.ValidationError(
                {name: [SERVER_FIELD_MESSAGE] for name in sorted(extra)}
            )
        return super().validate(attrs)


# --- fee schedules -----------------------------------------------------------------------------


class FeeScheduleSerializer(serializers.ModelSerializer):
    program = serializers.CharField(source="program_id", read_only=True)
    term = serializers.CharField(source="term_id", read_only=True)
    amount = MoneyField(read_only=True)
    supersedes = serializers.CharField(source="supersedes_id", read_only=True, allow_null=True)
    superseded_by = serializers.SerializerMethodField()
    effective_to = serializers.SerializerMethodField(
        help_text="Derived: the day before the next version's effective_from; null for the latest"
    )

    class Meta:
        model = FeeSchedule
        fields = [
            "fee_schedule_id",
            "program",
            "term",
            "fee_type",
            "residency",
            "charge_basis",
            "amount",
            "is_scholarship_eligible",
            "is_refundable",
            "is_waivable",
            "allocation_priority",
            "version",
            "effective_from",
            "effective_to",
            "supersedes",
            "superseded_by",
        ]
        read_only_fields = fields

    @staticmethod
    def _successor(obj):
        try:
            return obj.superseded_by
        except FeeSchedule.DoesNotExist:
            return None

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_superseded_by(self, obj):
        nxt = self._successor(obj)
        return nxt.fee_schedule_id if nxt else None

    @extend_schema_field(serializers.DateField(allow_null=True))
    def get_effective_to(self, obj):
        nxt = self._successor(obj)
        if nxt is None:
            return None
        return calc.effective_windows([obj, nxt])[obj.fee_schedule_id][1]


class FeeScheduleCreateSerializer(_ServerFieldsMixin, serializers.Serializer):
    program = serializers.PrimaryKeyRelatedField(
        queryset=Program.objects.all(), help_text="Program ID, e.g. PRG-001"
    )
    term = serializers.PrimaryKeyRelatedField(
        queryset=Term.objects.all(), help_text="Billable term ID, e.g. TERM-2026-FALL"
    )
    fee_type = serializers.ChoiceField(choices=FeeSchedule.FeeType.choices)
    residency = serializers.ChoiceField(
        choices=FeeSchedule.Residency.choices,
        help_text="TUITION: DOMESTIC or INTERNATIONAL; INTERNATIONAL_SERVICE: INTERNATIONAL; "
        "every other fee type: ALL",
    )
    charge_basis = serializers.ChoiceField(
        choices=FeeSchedule.ChargeBasis.choices, help_text="TUITION must be PER_CREDIT"
    )
    amount = MoneyField(min_value=0, help_text="INR per credit (PER_CREDIT) or per term (FLAT)")
    effective_from = serializers.DateField(help_text="A 2026 business date")


class FeeScheduleNewVersionSerializer(_ServerFieldsMixin, serializers.Serializer):
    amount = MoneyField(min_value=0)
    effective_from = serializers.DateField(
        help_text="Must be later than the predecessor's effective_from"
    )


# --- invoices --------------------------------------------------------------------------------


class WaiverSerializer(serializers.Serializer):
    fee_type = serializers.ChoiceField(
        choices=FeeSchedule.FeeType.choices,
        help_text="Only TECHNOLOGY, LIBRARY and STUDENT_ACTIVITY are waivable",
    )
    amount = MoneyField(min_value=0, help_text="0 ≤ amount ≤ the line's gross amount")
    reason = serializers.ChoiceField(
        choices=[(r, r) for r in policy.WAIVER_REASONS],
        required=False,
        allow_null=True,
        help_text="Required when amount > 0",
    )

    def to_internal_value(self, data):
        if isinstance(data, dict):
            extra = sorted(set(data) - set(self.fields))
            if extra:
                raise serializers.ValidationError({name: ["Unknown field."] for name in extra})
        return super().to_internal_value(data)


class InvoiceGenerateSerializer(StrictInputMixin, serializers.Serializer):
    enrollment_id = serializers.PrimaryKeyRelatedField(
        queryset=Enrollment.objects.all(), help_text="Enrollment ID, e.g. ENR-2026-000001"
    )
    waivers = serializers.ListField(
        child=WaiverSerializer(), required=False, default=list, help_text="BR-WAV-01"
    )


class InvoiceVoidSerializer(StrictInputMixin, serializers.Serializer):
    void_date = serializers.DateField(
        help_text="Required business date (2026), on or after the invoice issue date"
    )


class PaymentsByPayerSerializer(serializers.Serializer):
    SELF = MoneyField()
    GUARDIAN = MoneyField()
    SPONSOR = MoneyField()


class InvoiceAmountsSerializer(serializers.Serializer):
    """BR-INV-04: three kinds of reduction kept separate (computed, never stored)."""

    original_charges = MoneyField(help_text="Σ net of FEE + LATE_FEE lines (after waivers)")
    adjustments = MoneyField(help_text="Σ ADJUSTMENT lines (≤ 0); never payment or scholarship")
    net_charges = MoneyField()
    scholarship_applied = MoneyField(help_text="Σ net scholarship allocations (APPLY − REVERSE)")
    amount_paid = MoneyField(help_text="Every payer; 0.00 until payments exist (Phase 4)")
    payments_by_payer = PaymentsByPayerSerializer()
    outstanding = MoneyField()
    is_settled = serializers.BooleanField(help_text="outstanding = 0 (BR-INV-05)")


def amounts_data(amounts: calc.InvoiceAmounts) -> dict:
    return InvoiceAmountsSerializer(
        {
            "original_charges": amounts.original_charges,
            "adjustments": amounts.adjustments,
            "net_charges": amounts.net_charges,
            "scholarship_applied": amounts.scholarship_applied,
            "amount_paid": amounts.amount_paid,
            "payments_by_payer": {p: amounts.payments_by_payer.get(p, 0) for p in calc.PAYER_TYPES},
            "outstanding": amounts.outstanding,
            "is_settled": amounts.is_settled,
        }
    ).data


class InvoiceSerializer(serializers.ModelSerializer):
    """Invoice with the separated BR-INV-04 amounts and both statuses."""

    student = serializers.CharField(source="student_id", read_only=True)
    enrollment = serializers.CharField(source="enrollment_id", read_only=True)
    term = serializers.CharField(source="term_id", read_only=True)
    total_amount = MoneyField(read_only=True, help_text="Σ FEE line net at issue; immutable")
    amounts = serializers.SerializerMethodField()

    class Meta:
        model = Invoice
        fields = [
            "invoice_id",
            "student",
            "enrollment",
            "term",
            "issue_date",
            "due_date",
            "currency",
            "total_amount",
            "lifecycle_status",
            "collection_status",
            "collection_status_as_of",
            "amounts",
        ]
        read_only_fields = fields

    @extend_schema_field(InvoiceAmountsSerializer)
    def get_amounts(self, obj):
        from billing.services import current_amounts

        return amounts_data(current_amounts(obj, obj.lines.all()))


class InvoiceLineSerializer(serializers.ModelSerializer):
    fee_schedule = serializers.CharField(source="fee_schedule_id", read_only=True, allow_null=True)
    adjusts_line = serializers.CharField(source="adjusts_line_id", read_only=True, allow_null=True)
    quantity = DecimalStringField(max_digits=6, decimal_places=2, read_only=True)
    unit_amount = MoneyField(read_only=True)
    gross_amount = MoneyField(read_only=True)
    waiver_amount = MoneyField(read_only=True)
    net_amount = MoneyField(read_only=True)
    open_amount = serializers.SerializerMethodField(
        help_text="BR-ALC-02 open amount (FEE and LATE_FEE lines); null for ADJUSTMENT lines"
    )

    class Meta:
        model = InvoiceLine
        fields = [
            "line_id",
            "line_type",
            "fee_schedule",
            "fee_type",
            "description",
            "quantity",
            "unit_amount",
            "gross_amount",
            "waiver_amount",
            "waiver_reason",
            "net_amount",
            "adjusts_line",
            "adjustment_reason",
            "is_scholarship_eligible",
            "is_refundable",
            "is_waivable",
            "allocation_priority",
            "posted_date",
            "open_amount",
        ]
        read_only_fields = fields

    @extend_schema_field(MoneyField(allow_null=True))
    def get_open_amount(self, obj):
        value = self.context.get("open_amounts", {}).get(obj.line_id)
        return None if value is None else format_money(value)


class InvoiceDetailSerializer(InvoiceSerializer):
    lines = serializers.SerializerMethodField()

    class Meta(InvoiceSerializer.Meta):
        fields = [*InvoiceSerializer.Meta.fields, "lines"]
        read_only_fields = fields

    @extend_schema_field(InvoiceLineSerializer(many=True))
    def get_lines(self, obj):
        from billing.services import allocations_for

        lines = sorted(obj.lines.all(), key=lambda x: x.line_id)
        scholarship, payments = allocations_for(obj)
        opens = calc.line_open_amounts(lines, scholarship, payments)
        return InvoiceLineSerializer(lines, many=True, context={"open_amounts": opens}).data


class InvoiceEventSerializer(serializers.ModelSerializer):
    invoice = serializers.CharField(source="invoice_id", read_only=True)

    class Meta:
        model = InvoiceEvent
        fields = [
            "event_id",
            "invoice",
            "dimension",
            "from_status",
            "to_status",
            "event_date",
            "reason",
            "actor",
            "source_ref",
        ]
        read_only_fields = fields


# --- batch operations ------------------------------------------------------------------------


class LateFeeAssessSerializer(StrictInputMixin, serializers.Serializer):
    as_of_date = serializers.DateField(help_text="Required business date (2026); no default")
    dry_run = serializers.BooleanField(
        required=False, default=False, help_text="Report what would be posted; write nothing"
    )


class AssessedLateFeeSerializer(serializers.Serializer):
    invoice_id = serializers.CharField()
    outstanding = MoneyField(help_text="Outstanding at the end of due_date + grace days")
    late_fee = MoneyField()
    posted_date = serializers.DateField()
    line_id = serializers.CharField(allow_null=True, help_text="null in a dry run")


class LateFeeResultSerializer(serializers.Serializer):
    as_of_date = serializers.DateField()
    dry_run = serializers.BooleanField()
    examined = serializers.IntegerField()
    assessed_count = serializers.IntegerField()
    total_late_fees = MoneyField()
    assessed = AssessedLateFeeSerializer(many=True)


class CollectionRefreshSerializer(StrictInputMixin, serializers.Serializer):
    as_of_date = serializers.DateField(help_text="Required business date (2026); no default")


class CollectionChangeSerializer(serializers.Serializer):
    invoice_id = serializers.CharField()
    from_status = serializers.CharField()
    to_status = serializers.CharField()


class CollectionRefreshResultSerializer(serializers.Serializer):
    as_of_date = serializers.DateField()
    examined = serializers.IntegerField()
    changed = serializers.IntegerField()
    unchanged = serializers.IntegerField()
    skipped_later_as_of = serializers.IntegerField(
        help_text="Invoices whose status is already derived for a later date"
    )
    changes = CollectionChangeSerializer(many=True)


# --- student account views ---------------------------------------------------------------------


class LedgerEntrySerializer(serializers.ModelSerializer):
    student = serializers.CharField(source="student_id", read_only=True)
    amount = MoneyField(read_only=True)
    source_type = serializers.SerializerMethodField(
        help_text="INVOICE_LINE, SCHOLARSHIP_ALLOCATION, PAYMENT or REFUND"
    )
    invoice_line = serializers.CharField(source="invoice_line_id", read_only=True, allow_null=True)
    scholarship_allocation = serializers.CharField(
        source="scholarship_allocation_id", read_only=True, allow_null=True
    )
    payment = serializers.CharField(source="payment_id", read_only=True, allow_null=True)
    refund = serializers.CharField(source="refund_id", read_only=True, allow_null=True)
    invoice = serializers.SerializerMethodField(
        help_text="The invoice of a line or scholarship source; null for payments and refunds"
    )

    class Meta:
        model = LedgerEntry
        fields = [
            "entry_id",
            "student",
            "entry_date",
            "entry_type",
            "amount",
            "source_type",
            "invoice_line",
            "scholarship_allocation",
            "payment",
            "refund",
            "invoice",
            "memo",
        ]
        read_only_fields = fields

    def get_source_type(self, obj) -> str:
        if obj.scholarship_allocation_id:
            return "SCHOLARSHIP_ALLOCATION"
        if obj.payment_id:
            return "PAYMENT"
        if obj.refund_id:
            return "REFUND"
        return "INVOICE_LINE"

    def get_invoice(self, obj) -> str | None:
        if obj.invoice_line_id:
            return obj.invoice_line.invoice_id
        if obj.scholarship_allocation_id:  # reached through the relation; no app import
            return obj.scholarship_allocation.invoice_line.invoice_id
        return None


class AllocationRefSerializer(serializers.Serializer):
    """A scholarship allocation as billing shows it (read from the allocation registry)."""

    allocation_id = serializers.CharField()
    scholarship = serializers.CharField(source="scholarship_id")
    invoice_line = serializers.CharField(source="invoice_line_id")
    allocation_type = serializers.CharField(help_text="APPLY or REVERSE")
    amount = MoneyField(help_text="signed: APPLY > 0, REVERSE < 0")
    reason = serializers.CharField()
    allocated_on = serializers.DateField()


class PaymentAllocationRefSerializer(serializers.Serializer):
    """A payment allocation as billing shows it (read from the allocation registry)."""

    allocation_id = serializers.CharField()
    payment = serializers.CharField(source="payment_id")
    payer_type = serializers.CharField(help_text="SELF, GUARDIAN or SPONSOR (BR-PAY-03)")
    invoice_line = serializers.CharField(source="invoice_line_id")
    allocation_type = serializers.CharField(help_text="ALLOCATE or DEALLOCATE")
    amount = MoneyField(help_text="signed: ALLOCATE > 0, DEALLOCATE < 0")
    reverses = serializers.CharField(source="reverses_id", allow_null=True)
    rule_applied = serializers.CharField()
    allocated_on = serializers.DateField()


class RefundRefSerializer(serializers.Serializer):
    """A refund as billing shows it on a statement (read from the payments provider)."""

    refund_id = serializers.CharField()
    payment = serializers.CharField(source="payment_id")
    payer_type = serializers.CharField(help_text="Refunds go back to the source payment's payer")
    reason = serializers.CharField()
    amount = MoneyField()
    requested_date = serializers.DateField()
    processed_date = serializers.DateField(allow_null=True)
    status = serializers.CharField(help_text="REQUESTED, APPROVED, PROCESSED or REJECTED")


class StatementInvoiceSerializer(InvoiceDetailSerializer):
    scholarship_allocations = serializers.SerializerMethodField(
        help_text="APPLY and REVERSE scholarship allocations on this invoice's lines"
    )
    payment_allocations = serializers.SerializerMethodField(
        help_text="ALLOCATE and DEALLOCATE payment allocations on this invoice's lines"
    )

    class Meta(InvoiceDetailSerializer.Meta):
        fields = [
            *InvoiceDetailSerializer.Meta.fields,
            "scholarship_allocations",
            "payment_allocations",
        ]
        read_only_fields = fields

    @extend_schema_field(AllocationRefSerializer(many=True))
    def get_scholarship_allocations(self, obj):
        from billing.services import allocations_for

        scholarship, _ = allocations_for(obj)
        return AllocationRefSerializer(scholarship, many=True).data

    @extend_schema_field(PaymentAllocationRefSerializer(many=True))
    def get_payment_allocations(self, obj):
        from billing.services import allocations_for

        _, payments = allocations_for(obj)
        return PaymentAllocationRefSerializer(payments, many=True).data


class StatementSerializer(serializers.Serializer):
    student_id = serializers.CharField()
    invoices = StatementInvoiceSerializer(many=True)
    refunds = RefundRefSerializer(many=True, help_text="The student's refunds, any status")


class BalanceQuerySerializer(StrictInputMixin, serializers.Serializer):
    as_of = serializers.DateField(
        required=False, help_text="Include entries dated on or before this date (default: all)"
    )


class BalanceBreakdownSerializer(serializers.Serializer):
    charges = MoneyField()
    late_fees = MoneyField()
    adjustments = MoneyField()
    scholarship_credits = MoneyField(help_text="Σ SCHOLARSHIP_CREDIT entries (≤ 0)")
    scholarship_reversals = MoneyField(help_text="Σ SCHOLARSHIP_REVERSAL entries (≥ 0)")
    payments = MoneyField(help_text="Σ PAYMENT entries (≤ 0)")
    payments_by_payer = PaymentsByPayerSerializer(
        help_text="Σ PAYMENT entries per payer_type (≤ 0); every payer counts (BR-PAY-03)"
    )
    refunds = MoneyField(help_text="Σ REFUND entries (≥ 0): PROCESSED refunds only")


class BalanceIdentitySerializer(serializers.Serializer):
    rule_id = serializers.CharField()
    holds = serializers.BooleanField()
    ledger_balance = MoneyField()
    invoice_outstanding_minus_unallocated_credit = MoneyField()


class BalanceSerializer(serializers.Serializer):
    student_id = serializers.CharField()
    as_of = serializers.DateField(allow_null=True)
    ledger_balance = MoneyField(help_text="BR-BAL-01; negative = credit balance")
    breakdown = BalanceBreakdownSerializer()
    invoice_outstanding = MoneyField()
    unallocated_credit = MoneyField(
        help_text="BR-BAL-02, from the payment records: Σ payments − Σ net payment "
        "allocations − Σ PROCESSED refunds"
    )
    identity = BalanceIdentitySerializer()
