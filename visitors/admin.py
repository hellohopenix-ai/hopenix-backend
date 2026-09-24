from django.contrib import admin

from .models import Visitor


@admin.register(Visitor)
class VisitorAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "phone", "company", "meeting_with", "status", "reviewed", "created_by", "created_at")
    list_filter = ("status", "reviewed", "purpose", "appt_status")
    search_fields = ("code", "name", "phone", "company", "meeting_with")
    autocomplete_fields = ("created_by", "reviewed_by")
    readonly_fields = ("code", "created_at")
