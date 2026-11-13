from django.apps import AppConfig


class PaymentsConfig(AppConfig):
    name = "payments"
    verbose_name = "Payments"

    def ready(self):
        # D-S1 / D-P4: billing reads payment data through its registry and never imports this
        # app. Registration is idempotent (ready() may run more than once).
        from billing.allocations import (
            register_allocation_provider,
            register_payment_release_handler,
            register_student_provider,
        )
        from payments import services

        register_allocation_provider("payment", "payments", services.invoice_allocations)
        register_payment_release_handler("payments", services.release_for_scholarship)  # BR-ALC-06
        register_student_provider("student_credit", "payments", services.student_unallocated_credit)
        register_student_provider("refunds", "payments", services.student_refunds)
