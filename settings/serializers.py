from rest_framework import serializers

from .models import (
    BillingInfo,
    CompanySettings,
    ExpenseSettings,
    IncomeSettings,
    NotificationPreference,
    ProjectSettings,
    SalesSettings,
    SecuritySetting,
    TaskSettings,
)


def absolute_logo_url(obj, request=None):
    """Public URL of the company logo, or "" when none is set (the site then
    shows its built-in phoenix)."""
    if not obj.logo:
        return ""
    try:
        url = obj.logo.url
    except Exception:  # noqa: BLE001 — storage misconfigured / file gone
        return ""
    return request.build_absolute_uri(url) if request else url


class CompanySettingsSerializer(serializers.ModelSerializer):
    logo = serializers.SerializerMethodField()

    def get_logo(self, obj):
        return absolute_logo_url(obj, self.context.get("request"))

    class Meta:
        model = CompanySettings
        fields = [
            "name", "email", "phone", "website",
            "street", "city", "state", "zip_code", "country",
            "timezone", "currency", "date_format", "time_format",
            "language", "default_dashboard",
            "compact_mode", "email_notifications", "auto_currency_update",
            "logo", "updated_at",
        ]
        read_only_fields = ["updated_at", "logo"]


class NotificationPreferenceSerializer(serializers.ModelSerializer):
    # Frontend (SettingsPage.jsx) keys each notification row by `n.id`, not
    # `event_key` — expose it under that name so the payload shape matches
    # exactly what the Notifications tab already expects, no frontend
    # remapping needed.
    id = serializers.CharField(source="event_key")

    class Meta:
        model = NotificationPreference
        fields = ["id", "label", "email", "push", "sms"]


class SecuritySettingSerializer(serializers.ModelSerializer):
    """Read-only on purpose. `two_factor_enabled` can only change through the
    verified setup / disable endpoints (twofactor.py) — a client sending a
    flag must never be able to switch 2FA on or off."""

    backup_codes_left = serializers.SerializerMethodField()

    def get_backup_codes_left(self, obj):
        return len(obj.backup_codes or [])

    class Meta:
        model = SecuritySetting
        fields = ["two_factor_enabled", "two_factor_enabled_at", "backup_codes_left", "updated_at"]
        read_only_fields = fields


class BillingInfoSerializer(serializers.ModelSerializer):
    """What the Billing tab / Subscription card / storage bar read. The plan
    catalog and the history ride along so the browser holds no plan data of its
    own. `plan_name`, `price`, `storage_limit_gb` and `next_billing_date` are
    server-controlled: BillingInfoView derives them from the chosen plan."""

    history = serializers.SerializerMethodField()
    plans = serializers.SerializerMethodField()
    features = serializers.SerializerMethodField()

    def get_history(self, obj):
        from .models import BillingEvent

        return [
            {
                "id": e.id,
                "kind": e.kind,
                "date": e.created_at.date().isoformat(),
                "description": e.description,
                "amount": float(e.amount),
                "status": e.status,
            }
            for e in BillingEvent.objects.all()[:50]
        ]

    def get_plans(self, obj):
        from .plans import PLAN_CATALOG

        return PLAN_CATALOG

    def get_features(self, obj):
        from .plans import get_plan

        plan = get_plan(obj.plan_name)
        return plan["features"] if plan else []

    class Meta:
        model = BillingInfo
        fields = [
            "plan_name", "price", "billing_cycle",
            "card_brand", "card_last4", "card_expiry",
            "storage_used_gb", "storage_limit_gb",
            "next_billing_date", "features", "plans", "history",
            "updated_at",
        ]
        read_only_fields = fields


class ProjectSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProjectSettings
        fields = [
            "default_view", "auto_archive", "require_code", "allow_guest_access",
            "time_tracking", "categories", "updated_at",
        ]
        read_only_fields = ["updated_at"]


class TaskSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = TaskSettings
        fields = [
            "default_view", "auto_assign_lead", "allow_subtasks", "require_due_date",
            "send_reminders", "statuses", "updated_at",
        ]
        read_only_fields = ["updated_at"]


class IncomeSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = IncomeSettings
        fields = ["default_account", "recurring_income", "auto_invoice", "categories", "updated_at"]
        read_only_fields = ["updated_at"]


class ExpenseSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = ExpenseSettings
        fields = ["approval_threshold", "require_receipt", "auto_categorize", "categories", "updated_at"]
        read_only_fields = ["updated_at"]


class SalesSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = SalesSettings
        fields = [
            "tax_rate", "invoice_prefix", "payment_terms", "discount",
            "auto_invoice_number", "payment_reminders", "updated_at",
        ]
        read_only_fields = ["updated_at"]