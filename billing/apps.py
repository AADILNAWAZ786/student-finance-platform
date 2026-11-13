from django.apps import AppConfig


class BillingConfig(AppConfig):
    name = "billing"
    verbose_name = "Billing"

    def ready(self):
        # D-W1: academics orchestrates withdrawals and cancellations but may not import
        # billing; billing registers its exit handler with academics instead. Idempotent.
        from academics.exits import register_exit_handler
        from billing.withdrawals import apply_enrollment_exit

        register_exit_handler("billing", apply_enrollment_exit)
