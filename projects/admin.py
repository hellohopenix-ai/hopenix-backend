from django.contrib import admin
from .models import Project, Module, ModuleFile


class ModuleInline(admin.TabularInline):
    model = Module
    extra = 0


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ("name", "manager", "status", "is_archived", "updated_at")
    list_filter = ("status", "is_archived", "project_type")
    search_fields = ("name",)
    inlines = [ModuleInline]


@admin.register(Module)
class ModuleAdmin(admin.ModelAdmin):
    list_display = ("name", "project", "status", "unlocked", "due_date")
    list_filter = ("status", "unlocked")
    search_fields = ("name", "project__name")


admin.site.register(ModuleFile)