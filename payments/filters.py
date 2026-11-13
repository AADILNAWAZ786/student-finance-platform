import django_filters as df

from payments.models import Payment, PaymentAllocation, Refund


class PaymentFilter(df.FilterSet):
    student = df.CharFilter(field_name="student_id", help_text="Student ID")
    payer_type = df.ChoiceFilter(choices=Payment.PayerType.choices)
    method = df.ChoiceFilter(choices=Payment.Method.choices)
    external_reference = df.CharFilter()
    received_from = df.DateFilter(field_name="received_date", lookup_expr="gte")
    received_to = df.DateFilter(field_name="received_date", lookup_expr="lte")

    class Meta:
        model = Payment
        fields = [
            "student",
            "payer_type",
            "method",
            "external_reference",
            "received_from",
            "received_to",
        ]


class PaymentAllocationFilter(df.FilterSet):
    payment = df.CharFilter(field_name="payment_id", help_text="Payment ID")
    student = df.CharFilter(field_name="payment__student_id", help_text="Student ID")
    invoice = df.CharFilter(field_name="invoice_line__invoice_id", help_text="Invoice ID")
    invoice_line = df.CharFilter(field_name="invoice_line_id", help_text="Invoice line ID")
    allocation_type = df.ChoiceFilter(choices=PaymentAllocation.AllocationType.choices)
    rule_applied = df.ChoiceFilter(choices=PaymentAllocation.Rule.choices)

    class Meta:
        model = PaymentAllocation
        fields = [
            "payment",
            "student",
            "invoice",
            "invoice_line",
            "allocation_type",
            "rule_applied",
        ]


class RefundFilter(df.FilterSet):
    student = df.CharFilter(field_name="student_id", help_text="Student ID")
    payment = df.CharFilter(field_name="payment_id", help_text="Payment ID")
    status = df.ChoiceFilter(choices=Refund.Status.choices)
    reason = df.ChoiceFilter(choices=Refund.Reason.choices)

    class Meta:
        model = Refund
        fields = ["student", "payment", "status", "reason"]
