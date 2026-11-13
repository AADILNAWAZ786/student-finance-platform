"""Read-only admin: scholarship records are written only through services (IDs, events,
ledger entries); nothing can be added, changed or deleted here."""

from django.contrib import admin

from scholarships.models import (
    Eligibility,
    Scholarship,
    ScholarshipAllocation,
    ScholarshipEvent,
    ScholarshipType,
    ScholarshipTypeProgram,
)


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ScholarshipType)
class ScholarshipTypeAdmin(ReadOnlyAdmin):
    list_display = ("scholarship_type_id", "name", "category", "coverage_scope", "priority")


@admin.register(ScholarshipTypeProgram)
class ScholarshipTypeProgramAdmin(ReadOnlyAdmin):
    list_display = ("id", "scholarship_type", "program")


@admin.register(Eligibility)
class EligibilityAdmin(ReadOnlyAdmin):
    list_display = ("eligibility_id", "student", "scholarship_type", "term", "is_eligible")
    list_filter = ("is_eligible", "term")


@admin.register(Scholarship)
class ScholarshipAdmin(ReadOnlyAdmin):
    list_display = (
        "scholarship_id",
        "student",
        "scholarship_type",
        "term",
        "awarded_amount",
        "status",
    )
    list_filter = ("status", "term")


@admin.register(ScholarshipAllocation)
class ScholarshipAllocationAdmin(ReadOnlyAdmin):
    list_display = ("allocation_id", "scholarship", "invoice_line", "allocation_type", "amount")


@admin.register(ScholarshipEvent)
class ScholarshipEventAdmin(ReadOnlyAdmin):
    list_display = ("event_id", "scholarship", "from_status", "to_status", "event_date")
