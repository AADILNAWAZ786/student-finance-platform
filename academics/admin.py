"""Admin for academics. Programs, terms and enrollments are created through the API/services
(ID allocation, calendar and enrollment rules, first lifecycle event), so "add" is disabled.
Fields that pricing or enrollment rules depend on are read-only; nothing can be deleted."""

from django.contrib import admin

from academics.models import Enrollment, EnrollmentEvent, Program, Term


class _NoAddNoDelete(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Program)
class ProgramAdmin(_NoAddNoDelete):
    list_display = (
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
    )
    list_filter = ("level", "is_active", "is_stem", "has_lab", "department")
    search_fields = ("program_id", "code", "name", "department")
    ordering = ("program_id",)
    readonly_fields = (
        "program_id",
        "code",
        "level",
        "is_stem",
        "has_lab",
        "min_credits",
        "max_credits",
        "recorded_at",
    )


@admin.register(Term)
class TermAdmin(_NoAddNoDelete):
    list_display = (
        "term_id",
        "is_billable",
        "invoice_date",
        "start_date",
        "due_date",
        "census_date",
        "end_date",
        "late_fee_grace_days",
    )
    list_filter = ("is_billable", "year", "season")
    search_fields = ("term_id",)
    ordering = ("start_date",)

    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in Term._meta.fields]  # terms drive billing dates: view only


class EnrollmentEventInline(admin.TabularInline):
    model = EnrollmentEvent
    extra = 0
    can_delete = False
    fields = ("event_id", "event_date", "from_status", "to_status", "reason", "actor", "source_ref")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Enrollment)
class EnrollmentAdmin(_NoAddNoDelete):
    list_display = (
        "enrollment_id",
        "student",
        "program",
        "term",
        "credits",
        "status",
        "enrolled_on",
        "withdrawal_date",
        "cancellation_date",
    )
    list_filter = ("status", "term", "program")
    search_fields = ("enrollment_id", "student__student_id", "student__last_name")
    list_select_related = ("student", "program", "term")
    ordering = ("enrollment_id",)
    inlines = [EnrollmentEventInline]

    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in Enrollment._meta.fields]  # changes only via services/events
