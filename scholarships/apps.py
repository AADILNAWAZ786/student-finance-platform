from django.apps import AppConfig


class ScholarshipsConfig(AppConfig):
    name = "scholarships"
    verbose_name = "Scholarships"

    def ready(self):
        # D-S1: billing reads scholarship allocations through its registry; billing never
        # imports this app. Registration is idempotent (ready() may run more than once).
        from billing.allocations import register_allocation_provider
        from scholarships.services import invoice_allocations

        register_allocation_provider("scholarship", "scholarships", invoice_allocations)
