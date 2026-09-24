from django.conf import settings as django_settings
from django.db import models


class CompanySettings(models.Model):
    """Organization-wide settings (SettingsPage.jsx -> General tab).
    Singleton row (always pk=1) — get_or_create'd on first access, same
    pattern used for AppSetting in users/models.py."""

    name = models.CharField(max_length=255, blank=True, default="")
    email = models.EmailField(blank=True, default="")
    phone = models.CharField(max_length=50, blank=True, default="")
    website = models.CharField(max_length=255, blank=True, default="")

    street = models.CharField(max_length=255, blank=True, default="")
    city = models.CharField(max_length=120, blank=True, default="")
    state = models.CharField(max_length=120, blank=True, default="")
    zip_code = models.CharField(max_length=20, blank=True, default="")
    country = models.CharField(max_length=120, blank=True, default="")

    timezone = models.CharField(max_length=100, blank=True, default="UTC")
    currency = models.CharField(max_length=10, blank=True, default="USD")
    date_format = models.CharField(max_length=20, blank=True, default="MM/DD/YYYY")
    time_format = models.CharField(max_length=10, blank=True, default="12h")
    language = models.CharField(max_length=10, blank=True, default="en")
    default_dashboard = models.CharField(max_length=50, blank=True, default="overview")

    compact_mode = models.BooleanField(default=False)
    email_notifications = models.BooleanField(default=True)
    auto_currency_update = models.BooleanField(default=False)

    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self):
        return self.name or "Company Settings"


class NotificationPreference(models.Model):
    """Per-user, per-event-type notification channel toggles
    (SettingsPage.jsx -> Notifications tab). One row per (user, event_key)."""

    user = models.ForeignKey(
        django_settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="notification_preferences",
    )
    event_key = models.CharField(max_length=100)
    label = models.CharField(max_length=150, blank=True, default="")
    email = models.BooleanField(default=True)
    push = models.BooleanField(default=True)
    sms = models.BooleanField(default=False)

    class Meta:
        unique_together = ("user", "event_key")
        ordering = ["event_key"]

    def __str__(self):
        return f"{self.user_id} · {self.event_key}"


class SecuritySetting(models.Model):
    """Per-user security preferences (SettingsPage.jsx -> Security tab).
    Actual password changes go through ChangePasswordView, not this
    model — this only holds the 2FA toggle."""

    user = models.OneToOneField(
        django_settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="security_setting",
    )
    two_factor_enabled = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Security · {self.user_id}"


class BillingInfo(models.Model):
    """Organization-wide billing/plan info (SettingsPage.jsx -> Billing
    tab). Singleton row, same pattern as CompanySettings. NOTE: this
    stores only display data (plan name, masked card) — it does not talk
    to a real payment processor. Wire a provider like Stripe before
    using this for anything that needs to actually charge a card."""

    plan_name = models.CharField(max_length=100, blank=True, default="Free")
    price = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    billing_cycle = models.CharField(max_length=20, blank=True, default="monthly")

    card_brand = models.CharField(max_length=30, blank=True, default="")
    card_last4 = models.CharField(max_length=4, blank=True, default="")
    card_expiry = models.CharField(max_length=10, blank=True, default="")

    storage_used_gb = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    storage_limit_gb = models.DecimalField(max_digits=10, decimal_places=2, default=5)

    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self):
        return f"Billing · {self.plan_name}"


class ProjectSettings(models.Model):
    """SettingsPage.jsx -> Projects tab. Singleton, admin-editable. The
    Projects module's own app (projects/) owns actual Project records —
    this only holds organization-wide defaults/preferences for it."""

    default_view = models.CharField(max_length=30, blank=True, default="Kanban Board")
    auto_archive = models.BooleanField(default=True)
    require_code = models.BooleanField(default=False)
    allow_guest_access = models.BooleanField(default=False)
    time_tracking = models.BooleanField(default=True)
    # [{id, name, color}, ...] — same shape SettingsPage.jsx already
    # builds/edits via its CategoryList component, round-tripped as-is.
    categories = models.JSONField(default=list, blank=True)

    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self):
        return "Project Settings"


class TaskSettings(models.Model):
    """SettingsPage.jsx -> Tasks tab. Singleton, admin-editable."""

    default_view = models.CharField(max_length=30, blank=True, default="Board View")
    auto_assign_lead = models.BooleanField(default=False)
    allow_subtasks = models.BooleanField(default=True)
    require_due_date = models.BooleanField(default=False)
    send_reminders = models.BooleanField(default=True)
    # [{id, name, color}, ...] — the task-status pipeline (To Do/In
    # Progress/...), same list shape as ProjectSettings.categories.
    statuses = models.JSONField(default=list, blank=True)

    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self):
        return "Task Settings"


class IncomeSettings(models.Model):
    """SettingsPage.jsx -> Income tab. Singleton, admin-editable. The
    actual income RECORDS live in whichever app owns those (not this
    one) — this only holds preferences/category list for that module."""

    default_account = models.CharField(max_length=100, blank=True, default="")
    recurring_income = models.BooleanField(default=True)
    auto_invoice = models.BooleanField(default=False)
    categories = models.JSONField(default=list, blank=True)

    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self):
        return "Income Settings"


class ExpenseSettings(models.Model):
    """SettingsPage.jsx -> Expenses tab. Singleton, admin-editable.
    expenses/ owns actual Expense records; this only holds this
    module's preferences/category list."""

    approval_threshold = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    require_receipt = models.BooleanField(default=True)
    auto_categorize = models.BooleanField(default=False)
    categories = models.JSONField(default=list, blank=True)

    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self):
        return "Expense Settings"


class SalesSettings(models.Model):
    """SettingsPage.jsx -> Sales tab. Singleton, admin-editable.
    sales/ owns actual Sale/Invoice records; this only holds this
    module's preferences (tax rate, invoice numbering, terms)."""

    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    invoice_prefix = models.CharField(max_length=20, blank=True, default="INV-")
    payment_terms = models.CharField(max_length=50, blank=True, default="Net 15")
    discount = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    auto_invoice_number = models.BooleanField(default=True)
    payment_reminders = models.BooleanField(default=True)

    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self):
        return "Sales Settings"