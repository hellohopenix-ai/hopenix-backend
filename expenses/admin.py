from django.contrib import admin

from .models import Expense


@admin.register(Expense)
class ExpenseAdmin(admin.ModelAdmin):
    list_display = ("title", "category", "project", "amount", "date", "payment", "status", "created_by")
    list_filter = ("status", "category", "payment")
    search_fields = ("title", "project")
    autocomplete_fields = ("created_by",)
