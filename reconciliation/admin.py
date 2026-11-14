"""Read-only admin: snapshots, runs and findings are written only through
``reconciliation.services``; nothing can be added, changed or deleted here."""

from django.contrib import admin

from reconciliation.models import AnomalyFinding, BalanceSnapshot, ReconciliationRun


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(BalanceSnapshot)
class BalanceSnapshotAdmin(ReadOnlyAdmin):
    list_display = ("snapshot_id", "student", "as_of_date", "reported_balance")
    search_fields = ("snapshot_id", "student__student_id")


@admin.register(ReconciliationRun)
class ReconciliationRunAdmin(ReadOnlyAdmin):
    list_display = ("run_id", "scope", "student", "as_of_date", "status", "triggered_by")
    list_filter = ("status", "scope", "triggered_by")


@admin.register(AnomalyFinding)
class AnomalyFindingAdmin(ReadOnlyAdmin):
    list_display = ("finding_id", "run", "issue_type", "severity", "record_id", "student")
    list_filter = ("issue_type", "severity")
    search_fields = ("finding_id", "record_id", "student__student_id")
