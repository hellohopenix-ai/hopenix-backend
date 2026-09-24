from django.contrib import admin

from .models import Task


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ("title", "project", "status", "priority", "due_date", "client", "created_on")
    list_filter = ("status", "priority", "project")
    search_fields = ("title", "project", "assignees")
    autocomplete_fields = ("client",)
