from django.contrib import admin

from .models import CoworkingApplication, CoworkingMember, CoworkingPayment, CoworkingReview, CoworkingSettings


class MemberInline(admin.TabularInline):
    model = CoworkingMember
    extra = 0


class ReviewInline(admin.StackedInline):
    model = CoworkingReview
    extra = 0
    can_delete = False
    raw_id_fields = ("reviewed_by",)


class PaymentInline(admin.TabularInline):
    """Read-only on purpose: a tick and its Income row are created together
    by the API (views.py) and should only be undone by deleting the Income
    entry, not by editing one half here."""

    model = CoworkingPayment
    extra = 0
    can_delete = False
    readonly_fields = ("month", "amount", "method", "income", "recorded_by", "created_at")

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(CoworkingApplication)
class CoworkingApplicationAdmin(admin.ModelAdmin):
    list_display = ("code", "full_name", "company_name", "mobile", "chairs", "duration", "monthly_payment", "status", "created_at")
    list_filter = ("status", "seating", "billing_cycle")
    search_fields = ("code", "full_name", "company_name", "mobile", "cnic")
    readonly_fields = ("code", "monthly_payment", "subtotal", "discount_amount", "grand_total", "created_at", "updated_at")
    raw_id_fields = ("created_by",)
    # The photo / CNIC scans are deliberately left out: they live in private
    # storage with no public URL (see storage.py), which the admin's file
    # widgets can't render. View them through the authenticated API instead.
    exclude = ("photo", "cnic_front", "cnic_back")
    inlines = [MemberInline, ReviewInline, PaymentInline]


@admin.register(CoworkingSettings)
class CoworkingSettingsAdmin(admin.ModelAdmin):
    """Singleton (pk=1) -- edited from the Coworking Space page itself in
    normal use; this is a fallback for editing it straight from /admin/."""

    list_display = ("total_chairs", "monthly_rate", "min_duration_months", "max_duration_months", "updated_at")
    readonly_fields = ("updated_at",)

    def has_add_permission(self, request):
        return not CoworkingSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False