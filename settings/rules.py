"""Settings that actually change how the app behaves.

Each function reads the saved Settings-page values and is called from the one
place in the module it governs. Defaults are chosen so that an install which
never touched the Settings page behaves exactly as it did before.

  Tasks     require_due_date   a task needs a due date
            allow_subtasks     checklist items allowed on a task
            auto_assign_lead   a task with no assignee goes to its project's manager
  Expenses  approval_threshold amounts up to it are approved automatically (0 = off)
            require_receipt    an expense can't be Approved without a receipt
  Projects  auto_archive       projects Completed for 30+ days are archived

Not enforced (the app has no matching feature yet): project code, guest
access, time tracking, recurring income, auto-invoice, sales tax/prefix/terms,
payment reminders, task reminders, expense AI categorisation.
"""
import logging
from datetime import timedelta
from decimal import Decimal

from django.core.cache import cache
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .models import ExpenseSettings, ProjectSettings, TaskSettings

logger = logging.getLogger(__name__)

AUTO_ARCHIVE_AFTER_DAYS = 30


# ------------------------------------------------------------------ Tasks
def enforce_task_rules(validated_data, instance=None):
    """Raise a 400 when a task breaks the Tasks settings. `validated_data`
    is serializer.validated_data; `instance` the existing Task on update."""
    cfg = TaskSettings.load()
    errors = {}

    if cfg.require_due_date:
        if "due_date" in validated_data:
            due = validated_data.get("due_date")
        else:
            due = getattr(instance, "due_date", None)
        if not due:
            errors["dueDate"] = "A due date is required for every task (Settings → Tasks)."

    if not cfg.allow_subtasks and validated_data.get("subtasks"):
        errors["subtasks"] = "Subtasks are turned off (Settings → Tasks)."

    if errors:
        raise ValidationError(errors)


def default_assignees_for(validated_data):
    """auto_assign_lead: the project manager's name when nobody was assigned."""
    cfg = TaskSettings.load()
    if not cfg.auto_assign_lead or validated_data.get("assignees"):
        return None
    project_name = (validated_data.get("project") or "").strip()
    if not project_name:
        return None
    from projects.models import Project

    project = Project.objects.filter(name__iexact=project_name, is_archived=False).select_related("manager").first()
    manager = getattr(project, "manager", None)
    name = (getattr(manager, "name", "") or "").strip()
    return [name] if name else None


# --------------------------------------------------------------- Expenses
def apply_expense_create_rules(validated_data, has_receipt=False):
    """Returns extra fields to save with a new expense (auto-approval)."""
    cfg = ExpenseSettings.load()
    threshold = Decimal(cfg.approval_threshold or 0)
    amount = Decimal(validated_data.get("amount") or 0)
    if threshold > 0 and amount <= threshold and (has_receipt or not cfg.require_receipt):
        return {"status": "Approved"}
    return {}


def enforce_expense_approval(expense_status, has_receipt):
    """An expense can't be set to Approved without a receipt when receipts are mandatory."""
    cfg = ExpenseSettings.load()
    if cfg.require_receipt and expense_status == "Approved" and not has_receipt:
        raise ValidationError({"status": "A receipt must be uploaded before this expense can be approved (Settings → Expenses)."})


# --------------------------------------------------------------- Projects
def archive_old_completed_projects():
    """Archive projects that have been Completed for 30+ days. Returns how many."""
    if not ProjectSettings.load().auto_archive:
        return 0
    from projects.models import Project

    cutoff = timezone.now() - timedelta(days=AUTO_ARCHIVE_AFTER_DAYS)
    return Project.objects.filter(status="Completed", is_archived=False, updated_at__lt=cutoff).update(is_archived=True)


def maybe_archive(max_every_seconds=3600):
    """Cheap hook for request paths: runs the sweep at most once an hour."""
    try:
        if cache.add("settings.auto_archive.sweep", 1, max_every_seconds):
            n = archive_old_completed_projects()
            if n:
                logger.info("Auto-archived %s completed project(s)", n)
            return n
    except Exception:  # noqa: BLE001
        logger.exception("Auto-archive sweep failed")
    return 0
