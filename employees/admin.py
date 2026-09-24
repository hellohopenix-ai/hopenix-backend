from django.contrib import admin

from .models import Announcement, EmployeeExtra, Holiday, LeaveRequest


@admin.register(EmployeeExtra)
class EmployeeExtraAdmin(admin.ModelAdmin):
    list_display = ("user", "employment_status", "rating", "location", "updated_at")
    list_filter = ("employment_status", "rating")
    search_fields = ("user__name", "user__email")


@admin.register(LeaveRequest)
class LeaveRequestAdmin(admin.ModelAdmin):
    list_display = ("employee", "type", "start_date", "end_date", "days", "status", "requested_at")
    list_filter = ("status", "type")
    search_fields = ("employee__name", "employee__email")


@admin.register(Holiday)
class HolidayAdmin(admin.ModelAdmin):
    list_display = ("date", "reason", "created_by")
    ordering = ("date",)


@admin.register(Announcement)
class AnnouncementAdmin(admin.ModelAdmin):
    list_display = ("type", "employee", "detail", "created_at")
    list_filter = ("type",)
    search_fields = ("employee__name", "message", "detail")
