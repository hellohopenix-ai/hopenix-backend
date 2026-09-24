from django.contrib import admin
from .models import Client, Order, Expense, Income, Invoice, ModuleRequest, IntakeRequest, ActivityLogEntry, ClientMessage


@admin.register(Client)
class ClientAdmin(admin.ModelAdmin):
    list_display = (
        "name", "industry", "contact_person", "manager", "status",
        "has_portal_access", "total_orders_count", "total_spent", "created_at",
    )
    list_filter = ("status", "industry", "country")
    search_fields = ("name", "email", "contact_person", "phone", "country", "city")
    autocomplete_fields = ("manager", "portal_user", "created_by")

    @admin.display(boolean=True, description="Portal access")
    def has_portal_access(self, obj):
        return obj.portal_user_id is not None


@admin.register(ActivityLogEntry)
class ActivityLogEntryAdmin(admin.ModelAdmin):
    list_display = ("client", "text", "created_at")
    search_fields = ("client__name", "text")
    autocomplete_fields = ("client",)


@admin.register(ClientMessage)
class ClientMessageAdmin(admin.ModelAdmin):
    list_display = ("client", "sender", "kind", "subject", "created_at")
    list_filter = ("sender", "kind")
    search_fields = ("client__name", "text", "subject")
    autocomplete_fields = ("client",)


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ("client", "amount", "status", "created_at")
    list_filter = ("status", "client__country")
    search_fields = ("client__name",)
    autocomplete_fields = ("client",)


@admin.register(Expense)
class ExpenseAdmin(admin.ModelAdmin):
    list_display = ("title", "category", "amount", "date")
    list_filter = ("category",)
    search_fields = ("title",)


@admin.register(Income)
class IncomeAdmin(admin.ModelAdmin):
    list_display = ("description", "project", "client", "amount", "method", "status", "date", "received_on")
    list_filter = ("status", "method", "project")
    search_fields = ("description", "project", "client")


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = ("client", "project", "milestone_number", "milestone_total", "amount", "status", "created_at")
    list_filter = ("status",)
    search_fields = ("client__name",)
    autocomplete_fields = ("client", "project")


@admin.register(ModuleRequest)
class ModuleRequestAdmin(admin.ModelAdmin):
    list_display = ("client", "module_name", "status", "requested_at", "decided_at")
    list_filter = ("status",)
    search_fields = ("client__name", "module__name", "custom_module_name")
    autocomplete_fields = ("client", "module", "project")


@admin.register(IntakeRequest)
class IntakeRequestAdmin(admin.ModelAdmin):
    list_display = ("company_name", "contact_person", "email", "status", "created_at")
    list_filter = ("status",)
    search_fields = ("company_name", "contact_person", "email")