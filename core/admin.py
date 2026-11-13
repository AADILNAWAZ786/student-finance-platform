from django.contrib import admin

from core.models import IdSequence


@admin.register(IdSequence)
class IdSequenceAdmin(admin.ModelAdmin):
    list_display = ("key", "last_value")
    search_fields = ("key",)
    readonly_fields = ("key", "last_value")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
