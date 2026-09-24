from django.contrib import admin

from .models import Sale


@admin.register(Sale)
class SaleAdmin(admin.ModelAdmin):
    list_display = ("client", "project", "amount", "date", "status", "created_by", "created_at")
    list_filter = ("status", "date")
    search_fields = ("client", "project")
    date_hierarchy = "date"
