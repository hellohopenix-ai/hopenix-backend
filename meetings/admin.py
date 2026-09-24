from django.contrib import admin

from .models import Availability, Meeting, MeetingRequest, RescheduleRequest


@admin.register(Meeting)
class MeetingAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "type", "project", "raw_date", "raw_time", "status", "created_by")
    list_filter = ("type", "status", "raw_date")
    search_fields = ("title", "project", "created_by")
    ordering = ("-raw_date", "-raw_time")


@admin.register(MeetingRequest)
class MeetingRequestAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "role", "project", "raw_date", "raw_time", "status", "created_at")
    list_filter = ("status", "role")
    search_fields = ("name", "project", "reason")


@admin.register(RescheduleRequest)
class RescheduleRequestAdmin(admin.ModelAdmin):
    list_display = ("id", "meeting", "name", "raw_date", "raw_time", "status", "created_at")
    list_filter = ("status",)
    search_fields = ("name", "project", "reason")


@admin.register(Availability)
class AvailabilityAdmin(admin.ModelAdmin):
    list_display = ("id", "active", "start_time", "end_time", "days")
