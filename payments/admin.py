"""Read-only admin: payments, allocations, refunds and refund events are written only through
``payments.services`` (IDs, ledger entries, caps, events); nothing can be added, changed or
deleted here."""

from django.contrib import admin

from payments.models import Payment, PaymentAllocation, Refund, RefundEvent


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Payment)
class PaymentAdmin(ReadOnlyAdmin):
    list_display = ("payment_id", "student", "amount", "received_date", "method", "payer_type")
    list_filter = ("payer_type", "method")
    search_fields = ("payment_id", "external_reference", "student__student_id")


@admin.register(PaymentAllocation)
class PaymentAllocationAdmin(ReadOnlyAdmin):
    list_display = (
        "allocation_id",
        "payment",
        "invoice_line",
        "allocation_type",
        "amount",
        "rule_applied",
        "allocated_on",
    )
    list_filter = ("allocation_type", "rule_applied")


@admin.register(Refund)
class RefundAdmin(ReadOnlyAdmin):
    list_display = ("refund_id", "student", "payment", "amount", "status", "requested_date")
    list_filter = ("status", "reason")


@admin.register(RefundEvent)
class RefundEventAdmin(ReadOnlyAdmin):
    list_display = ("event_id", "refund", "from_status", "to_status", "event_date")
