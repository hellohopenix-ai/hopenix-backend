import os
import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

from .constants import CATEGORIES


class ActivityLog(models.Model):
    """One row = one thing a user did on the website.

    Append-only audit trail. Rows are written automatically (see
    reports/signals.py for model create/update/delete, reports/middleware.py
    for request-level actions, and the explicit hooks in users/views.py for
    login/logout) and are never edited afterwards.

    Actor details are SNAPSHOTTED (actor_name/email/role) next to the FK so
    the history still reads correctly after an account is removed — the FK
    goes NULL on user deletion, the snapshot stays.
    """

    ACTION_LABELS = {
        "create": "Created",
        "update": "Updated",
        "delete": "Deleted",
        "approve": "Approved",
        "reject": "Rejected",
        "upload": "Uploaded",
        "download": "Downloaded",
        "export": "Exported",
        "login": "Logged in",
        "logout": "Logged out",
        "login_failed": "Failed login",
        "register": "Registered",
        "view": "Viewed",
        "click": "Clicked",
        "search": "Searched",
        "api": "Other action",
    }
    ACTION_CHOICES = list(ACTION_LABELS.items())

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="activity_logs"
    )
    actor_name = models.CharField(max_length=255, blank=True, default="")
    actor_email = models.CharField(max_length=254, blank=True, default="")
    actor_role = models.CharField(max_length=20, blank=True, default="")

    action = models.CharField(max_length=20, choices=ACTION_CHOICES, db_index=True)
    module = models.CharField(max_length=30, default="General", db_index=True)

    object_type = models.CharField(max_length=60, blank=True, default="")
    object_id = models.CharField(max_length=64, blank=True, default="")
    object_repr = models.CharField(max_length=255, blank=True, default="")
    description = models.CharField(max_length=500, blank=True, default="")
    # Project name when the action belongs to one — lets the Reports page
    # filter activity by project exactly like it filters daily reports.
    project = models.CharField(max_length=255, blank=True, default="", db_index=True)

    # {"status": {"from": "Pending", "to": "Completed"}} — sensitive values redacted.
    changes = models.JSONField(default=dict, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    method = models.CharField(max_length=8, blank=True, default="")
    path = models.CharField(max_length=255, blank=True, default="")
    status_code = models.PositiveSmallIntegerField(null=True, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=255, blank=True, default="")

    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["user", "-created_at"], name="act_user_time_idx"),
            models.Index(fields=["module", "-created_at"], name="act_module_time_idx"),
            models.Index(fields=["action", "-created_at"], name="act_action_time_idx"),
        ]

    def __str__(self):
        return f"{self.actor_name or 'System'}: {self.description}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise PermissionError("Activity log entries are append-only and cannot be modified.")
        super().save(*args, **kwargs)


def daily_report_file_path(instance, filename):
    # media/reports/daily/<user_id>/<yyyy-mm-dd>/<uuid>_<filename> — real bytes on
    # disk, only this relative path in Postgres (same pattern as
    # projects.module_file_path / expenses.expense_receipt_path).
    report = instance.report
    return f"reports/daily/{report.user_id or 'anon'}/{report.date}/{uuid.uuid4()}_{os.path.basename(filename)}"


class DailyReport(models.Model):
    """ReportsPage.jsx's "Upload Daily Report": what one employee worked on
    today, optionally with photo/video proof. Replaces the old
    localStorage + IndexedDB storage — files now live on disk (see
    DailyReportFile) instead of inside one browser."""

    STATUS_CHOICES = [("pending", "Pending"), ("approved", "Approved")]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="daily_reports"
    )
    # Snapshot, same idea as ActivityLog.actor_* — survives account removal.
    user_name = models.CharField(max_length=255, blank=True, default="")
    user_email = models.CharField(max_length=254, blank=True, default="")

    date = models.DateField(default=timezone.localdate)
    note = models.TextField(blank=True, default="")
    # Free-text project name, exactly like tasks.Task.project / sales.Sale.project.
    project = models.CharField(max_length=255, blank=True, default="", db_index=True)

    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="pending")
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    approved_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["user", "-date"], name="daily_user_date_idx"),
            models.Index(fields=["status"], name="daily_status_idx"),
        ]

    def __str__(self):
        return f"{self.user_name or 'Unknown'} — {self.date}"


class DailyReportFile(models.Model):
    report = models.ForeignKey(DailyReport, on_delete=models.CASCADE, related_name="files")
    file = models.FileField(upload_to=daily_report_file_path)
    original_name = models.CharField(max_length=255)
    content_type = models.CharField(max_length=100, blank=True, default="")
    size = models.PositiveBigIntegerField(default=0)
    kind = models.CharField(max_length=10, default="image")  # image | video
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return self.original_name


class ReportOverride(models.Model):
    """Rename / delete of one of the auto-generated rows in "All Reports".
    Those rows are computed live from real data (see reports/catalog.py), so
    "delete" just hides the key and "rename" stores a display name."""

    key = models.CharField(max_length=100, unique=True)
    custom_name = models.CharField(max_length=255, blank=True, default="")
    hidden = models.BooleanField(default=False)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.key} ({'hidden' if self.hidden else self.custom_name or 'default'})"


class CustomReport(models.Model):
    """A row an admin added by hand — "Duplicate" or "Generate Custom Report"
    on the Reports page. (Auto-generated rows are never stored; see ReportOverride.)"""

    FORMAT_CHOICES = [("PDF", "PDF"), ("Excel", "Excel")]

    name = models.CharField(max_length=255)
    category = models.CharField(max_length=20, choices=[(c, c) for c in CATEGORIES])
    description = models.TextField(blank=True, default="")
    format = models.CharField(max_length=10, choices=FORMAT_CHOICES, default="PDF")
    source_key = models.CharField(max_length=100, blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return self.name
