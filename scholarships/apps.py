from django.apps import AppConfig


class ScholarshipsConfig(AppConfig):
    name = "scholarships"
    verbose_name = "Scholarships"

    def ready(self):
        # D-S1: billing reads scholarship allocations through its registry; billing never
        # imports this app. Registration is idempotent (ready() may run more than once).
        from billing.allocations import register_adjustment_handler, register_allocation_provider
        from scholarships.services import invoice_allocations, reverse_for_adjustment

        register_allocation_provider("scholarship", "scholarships", invoice_allocations)
        # Phase 5 (BR-WDR-03 step 1 / BR-SCH-07): reversal when a line is adjusted
        register_adjustment_handler("scholarship_reversal", "scholarships", reverse_for_adjustment)
