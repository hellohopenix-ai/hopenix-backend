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


class CompanySettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = CompanySettings
        fields = [
            "name", "email", "phone", "website",
            "street", "city", "state", "zip_code", "country",
            "timezone", "currency", "date_format", "time_format",
            "language", "default_dashboard",
            "compact_mode", "email_notifications", "auto_currency_update",
            "updated_at",
        ]
        read_only_fields = ["updated_at"]


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
    class Meta:
        model = SecuritySetting
        fields = ["two_factor_enabled", "updated_at"]
        read_only_fields = ["updated_at"]


class BillingInfoSerializer(serializers.ModelSerializer):
    class Meta:
        model = BillingInfo
        fields = [
            "plan_name", "price", "billing_cycle",
            "card_brand", "card_last4", "card_expiry",
            "storage_used_gb", "storage_limit_gb",
            "updated_at",
        ]
        read_only_fields = ["updated_at"]


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