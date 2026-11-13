"""Admin for students. Creation goes through the API/services (ID allocation + first status
event), so "add" is disabled; status is changed only via POST /students/{id}/status/.
Contact fields stay editable. Nothing can be deleted (history is append-only)."""

from django.contrib import admin

from students.models import Student, StudentStatusEvent


class StudentStatusEventInline(admin.TabularInline):
    model = StudentStatusEvent
    extra = 0
    can_delete = False
    fields = ("event_id", "event_date", "from_status", "to_status", "reason", "actor", "source_ref")
    readonly_fields = fields
    ordering = ("event_date", "event_id")

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Student)
class StudentAdmin(admin.ModelAdmin):
    list_display = (
        "student_id",
        "last_name",
        "first_name",
        "email",
        "program",
        "residency",
        "status",
        "gpa",
        "need_index",
        "admit_term",
    )
    list_filter = ("status", "residency", "program", "admit_term")
    search_fields = ("student_id", "first_name", "last_name", "email")
    list_select_related = ("program", "admit_term")
    ordering = ("student_id",)
    readonly_fields = (
        "student_id",
        "date_of_birth",
        "residency",
        "program",
        "admit_term",
        "gpa",
        "need_index",
        "status",
        "recorded_at",
    )
    fieldsets = (
        (
            "Identity",
            {"fields": ("student_id", "first_name", "last_name", "email", "date_of_birth")},
        ),
        ("Academic", {"fields": ("program", "admit_term", "residency", "gpa", "need_index")}),
        ("Lifecycle", {"fields": ("status", "recorded_at")}),
    )
    inlines = [StudentStatusEventInline]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
