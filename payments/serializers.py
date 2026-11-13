"""Payments serializers. Money is a string (ARCHITECTURE §9.1); input is strict (unknown fields
are rejected), so a client can never send a server-controlled value such as ``status``,
``currency``, ``payer_type`` on a refund or a refund's ``calculation``."""

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from billing.models import InvoiceLine
from core.fields import MoneyField
from core.money import dsum
from core.serializers import StrictInputMixin
from payments import calculators as calc
from payments import policy
from payments.models import Payment, PaymentAllocation, Refund, RefundEvent
from students.models import Student

POSITIVE = "Must be greater than 0."


def _positive(value):
    if value <= 0:
        raise serializers.ValidationError(POSITIVE)
    return value


# --- read serializers -----------------------------------------------------------------------------


class PaymentAllocationSerializer(serializers.ModelSerializer):
    payment = serializers.CharField(source="payment_id", read_only=True)
    payer_type = serializers.CharField(read_only=True, help_text="The payment's payer")
    invoice = serializers.CharField(source="invoice_line.invoice_id", read_only=True)
    invoice_line = serializers.CharField(source="invoice_line_id", read_only=True)
    amount = MoneyField(read_only=True, help_text="signed: ALLOCATE > 0, DEALLOCATE < 0")
    reverses = serializers.CharField(source="reverses_id", read_only=True, allow_null=True)

    class Meta:
        model = PaymentAllocation
        fields = [
            "allocation_id",
            "payment",
            "payer_type",
            "invoice",
            "invoice_line",
            "allocation_type",
            "amount",
            "reverses",
            "rule_applied",
            "allocated_on",
        ]
        read_only_fields = fields


class PaymentSerializer(serializers.ModelSerializer):
    student = serializers.CharField(source="student_id", read_only=True)
    amount = MoneyField(read_only=True)
    net_allocated = serializers.SerializerMethodField(help_text="Σ ALLOCATE − Σ DEALLOCATE")
    refunded = serializers.SerializerMethodField(
        help_text="Σ refunds not REJECTED (REQUESTED / APPROVED reserve, PROCESSED paid out)"
    )
    available = serializers.SerializerMethodField(
        help_text="amount − net_allocated − refunded: what can still be allocated or refunded"
    )
    allocation_state = serializers.SerializerMethodField(
        help_text="UNALLOCATED, PARTIAL or FULL (net_allocated vs amount)"
    )

    class Meta:
        model = Payment
        fields = [
            "payment_id",
            "student",
            "amount",
            "currency",
            "received_date",
            "method",
            "payer_type",
            "payer_name",
            "external_reference",
            "status",
            "net_allocated",
            "refunded",
            "available",
            "allocation_state",
        ]
        read_only_fields = fields

    def _figures(self, obj):
        cache = getattr(obj, "_figures", None)
        if cache is None:
            allocs, refunds = list(obj.allocations.all()), list(obj.refunds.all())
            net = calc.net_allocated(allocs)
            cache = obj._figures = {
                "net": net,
                "refunded": dsum(r.amount for r in refunds if r.status != policy.REJECTED),
                "available": calc.payment_available(obj.amount, allocs, refunds),
                "state": calc.allocation_state(obj.amount, net),
            }
        return cache

    @extend_schema_field(MoneyField())
    def get_net_allocated(self, obj):
        return f"{self._figures(obj)['net']:.2f}"

    @extend_schema_field(MoneyField())
    def get_refunded(self, obj):
        return f"{self._figures(obj)['refunded']:.2f}"

    @extend_schema_field(MoneyField())
    def get_available(self, obj):
        return f"{self._figures(obj)['available']:.2f}"

    @extend_schema_field(serializers.ChoiceField(choices=["UNALLOCATED", "PARTIAL", "FULL"]))
    def get_allocation_state(self, obj):
        return self._figures(obj)["state"]


class PaymentDetailSerializer(PaymentSerializer):
    allocations = serializers.SerializerMethodField()

    class Meta(PaymentSerializer.Meta):
        fields = [*PaymentSerializer.Meta.fields, "allocations"]
        read_only_fields = fields

    @extend_schema_field(PaymentAllocationSerializer(many=True))
    def get_allocations(self, obj):
        rows = sorted(obj.allocations.all(), key=lambda a: a.allocation_id)
        return PaymentAllocationSerializer(rows, many=True).data


class AllocationResultSerializer(serializers.Serializer):
    mode = serializers.ChoiceField(choices=["AUTO", "MANUAL"])
    outcome = serializers.ChoiceField(
        choices=list(policy.OUTCOMES),
        help_text="ALLOCATED; NO_OPEN_LINES (no row written, the money stays unallocated "
        "credit, BR-ALC-04); NO_FUNDS_AVAILABLE (nothing left on the payment)",
    )
    allocated_on = serializers.DateField()
    available_before = MoneyField()
    allocated = MoneyField()
    unallocated_after = MoneyField(help_text="Left on the payment as unallocated credit")
    allocations = PaymentAllocationSerializer(many=True, help_text="Rows written by this call")


class PaymentCreatedSerializer(PaymentDetailSerializer):
    auto_allocation = serializers.SerializerMethodField(
        help_text="The AUTO allocation result when auto_allocate was true, else null"
    )

    class Meta(PaymentDetailSerializer.Meta):
        fields = [*PaymentDetailSerializer.Meta.fields, "auto_allocation"]
        read_only_fields = fields

    @extend_schema_field(AllocationResultSerializer(allow_null=True))
    def get_auto_allocation(self, obj):
        result = self.context.get("auto_allocation")
        return AllocationResultSerializer(result).data if result else None


class AllocateResponseSerializer(AllocationResultSerializer):
    payment = PaymentSerializer()


class RefundEventSerializer(serializers.ModelSerializer):
    refund = serializers.CharField(source="refund_id", read_only=True)

    class Meta:
        model = RefundEvent
        fields = [
            "event_id",
            "refund",
            "from_status",
            "to_status",
            "event_date",
            "reason",
            "actor",
            "source_ref",
        ]
        read_only_fields = fields


class RefundSerializer(serializers.ModelSerializer):
    student = serializers.CharField(source="student_id", read_only=True)
    payment = serializers.CharField(source="payment_id", read_only=True)
    enrollment = serializers.CharField(source="enrollment_id", read_only=True, allow_null=True)
    amount = MoneyField(read_only=True)
    calculation = serializers.DictField(
        read_only=True,
        help_text="Request-time audit snapshot (never rewritten): net_paid, credit_balance, "
        "payment_available, schedule_pct, pending_refunds, credit_available, max_refundable",
    )

    class Meta:
        model = Refund
        fields = [
            "refund_id",
            "student",
            "payment",
            "payer_type",
            "enrollment",
            "reason",
            "amount",
            "requested_date",
            "processed_date",
            "status",
            "calculation",
        ]
        read_only_fields = fields


class RefundDetailSerializer(RefundSerializer):
    events = serializers.SerializerMethodField()

    class Meta(RefundSerializer.Meta):
        fields = [*RefundSerializer.Meta.fields, "events"]
        read_only_fields = fields

    @extend_schema_field(RefundEventSerializer(many=True))
    def get_events(self, obj):
        rows = sorted(obj.events.all(), key=lambda e: (e.event_date, e.event_id))
        return RefundEventSerializer(rows, many=True).data


class RefundablePaymentSerializer(serializers.Serializer):
    payment_id = serializers.CharField()
    payer_type = serializers.CharField()
    received_date = serializers.DateField()
    amount = MoneyField()
    net_allocated = MoneyField()
    refunds_reserved_or_processed = MoneyField(help_text="Σ this payment's refunds not REJECTED")
    available = MoneyField(help_text="BR-REF-02 per-payment cap")
    max_refundable = MoneyField(help_text="max_refundable(net_paid, credit_available, available)")


class RefundableCalculationSerializer(serializers.Serializer):
    net_paid = MoneyField(help_text="BR-REF-01: Σ payments − Σ refunds not REJECTED")
    ledger_balance = MoneyField()
    credit_balance = MoneyField(help_text="BR-REF-02: −ledger balance when negative, else 0")
    pending_refunds = MoneyField(help_text="Σ REQUESTED + APPROVED refunds (reserved)")
    credit_available = MoneyField(help_text="credit_balance − pending_refunds, ≥ 0")
    payments_available = MoneyField(help_text="Σ available of the student's payments")


class RefundableSerializer(serializers.Serializer):
    student_id = serializers.CharField()
    max_refundable = MoneyField(
        help_text="max_refundable(net_paid, credit_available, payments_available)"
    )
    calculation = RefundableCalculationSerializer()
    payments = RefundablePaymentSerializer(many=True)


# --- input serializers ----------------------------------------------------------------------------


class PaymentCreateSerializer(StrictInputMixin, serializers.Serializer):
    student_id = serializers.PrimaryKeyRelatedField(
        queryset=Student.objects.all(), help_text="Student ID, e.g. STU-000001"
    )
    amount = MoneyField(validators=[_positive], help_text="> 0 (BR-PAY-01)")
    received_date = serializers.DateField(
        help_text="2026 business date, on or after the student's first invoice issue date"
    )
    method = serializers.ChoiceField(choices=Payment.Method.choices)
    payer_type = serializers.ChoiceField(
        choices=Payment.PayerType.choices, help_text="Every payer counts as money paid (BR-PAY-03)"
    )
    payer_name = serializers.CharField(max_length=80, required=False, allow_null=True)
    external_reference = serializers.CharField(
        max_length=40, help_text="Unique; resubmitting returns 409 DUPLICATE_PAYMENT (BR-PAY-01)"
    )
    auto_allocate = serializers.BooleanField(
        required=False, default=False, help_text="AUTO-allocate in the same transaction"
    )


class AllocationLineSerializer(serializers.Serializer):
    invoice_line_id = serializers.PrimaryKeyRelatedField(
        queryset=InvoiceLine.objects.select_related("invoice"),
        help_text="A FEE or LATE_FEE line of the payment's student",
    )
    amount = MoneyField(validators=[_positive])

    def to_internal_value(self, data):
        # nested items have no initial_data of their own, so StrictInputMixin cannot see their
        # keys: reject unknown fields here (as billing's WaiverSerializer does)
        if isinstance(data, dict):
            extra = sorted(set(data) - set(self.fields))
            if extra:
                raise serializers.ValidationError({name: ["Unknown field."] for name in extra})
        return super().to_internal_value(data)


class AllocateSerializer(StrictInputMixin, serializers.Serializer):
    mode = serializers.ChoiceField(choices=["AUTO", "MANUAL"])
    lines = AllocationLineSerializer(
        many=True, required=False, help_text="MANUAL only: 1–99 distinct lines"
    )
    allocated_on = serializers.DateField(
        required=False,
        help_text="Required for MANUAL; AUTO defaults to the payment's received_date",
    )

    def validate(self, attrs):
        attrs = super().validate(attrs)
        lines = attrs.get("lines")
        if attrs["mode"] == "AUTO":
            if lines is not None:
                raise serializers.ValidationError({"lines": ["Must be omitted when mode is AUTO."]})
            return attrs
        errors = {}
        if not lines:
            errors["lines"] = ["Required (1–99 lines) when mode is MANUAL."]
        elif len(lines) > policy.MAX_ALLOCATIONS_PER_PAYMENT:
            errors["lines"] = [f"At most {policy.MAX_ALLOCATIONS_PER_PAYMENT} lines per request."]
        if "allocated_on" not in attrs:
            errors["allocated_on"] = ["Required when mode is MANUAL."]
        if errors:
            raise serializers.ValidationError(errors)
        return attrs


class RefundCreateSerializer(StrictInputMixin, serializers.Serializer):
    payment_id = serializers.PrimaryKeyRelatedField(
        queryset=Payment.objects.all(), help_text="Source payment; the refund goes to its payer"
    )
    amount = MoneyField(validators=[_positive])
    requested_date = serializers.DateField(help_text="On or after the payment's received_date")
    reason = serializers.ChoiceField(
        choices=[(r, r) for r in policy.API_REFUND_REASONS],
        required=False,
        default=policy.OVERPAYMENT,
        help_text="OVERPAYMENT (withdrawal and cancellation refunds arrive with Phase 5)",
    )


class RefundStatusDateSerializer(StrictInputMixin, serializers.Serializer):
    on_date = serializers.DateField(help_text="Business date of the change (RefundEvent date)")


class RefundProcessSerializer(StrictInputMixin, serializers.Serializer):
    processed_date = serializers.DateField(
        help_text="On or after the approval date; no later than 2026-12-31"
    )
