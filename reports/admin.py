from django.contrib import admin

from .models import ActivityLog, CustomReport, DailyReport, DailyReportFile, ReportOverride


@admin.register(ActivityLog)
class ActivityLogAdmin(admin.ModelAdmin):
    """Read-only: an audit trail nobody can edit or add to by hand."""

    list_display = ("created_at", "actor_name", "action", "module", "description", "ip_address")
    list_filter = ("action", "module", "created_at")
    search_fields = ("actor_name", "actor_email", "description", "object_repr", "project")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        # Retention is handled by `manage.py purge_activity`, not by clicking.
        return False


class DailyReportFileInline(admin.TabularInline):
    model = DailyReportFile
    extra = 0
    readonly_fields = ("original_name", "content_type", "size", "kind")


@admin.register(DailyReport)
class DailyReportAdmin(admin.ModelAdmin):
    list_display = ("user_name", "date", "project", "status", "created_at")
    list_filter = ("status", "date")
    search_fields = ("user_name", "user_email", "note", "project")
    inlines = [DailyReportFileInline]


admin.site.register(ReportOverride)
admin.site.register(CustomReport)
