"""Automatic activity recording.

Every model listed in TRACKED below gets pre_save / post_save / post_delete
handlers. When one of those models is created, changed or deleted while an HTTP
request is being served, one ActivityLog row is written, attributed to the
user behind that request (see reports/context.py + reports/middleware.py).

Because this hangs off the ORM, it covers every view that already exists —
and every view added later — without touching their code. To start recording
a new model, add one line to TRACKED.

Not covered on purpose (Django skips signals for them): queryset.update(),
bulk_create() and raw SQL. That's also why presence pings
(User.touch_active) and unread counters don't flood the log.
"""

import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional, Tuple

from django.apps import apps
from django.db import transaction
from django.db.models.signals import m2m_changed, post_delete, post_save, pre_save

from .constants import IGNORED_FIELDS
from .context import get_state
from .services import clip, is_sensitive, log_activity, normalize, shorten, user_label

logger = logging.getLogger(__name__)

APPROVE_WORDS = {"approved", "accepted"}
REJECT_WORDS = {"rejected", "declined", "denied"}


@dataclass
class Track:
    module: str                     # Reports-page grouping, e.g. "Tasks"
    label: str                      # how it reads in a sentence: "task", "leave request"
    repr: Any = None                # attr path ("name", "project.name") or callable(instance)
    project: Any = None             # same, resolving to a project NAME (for project filtering)
    events: Tuple[str, ...] = ("create", "update", "delete")
    only: Tuple[str, ...] = ()      # if set, only these fields are ever diffed
    exclude: Tuple[str, ...] = ()   # fields never diffed
    summary: Tuple[str, ...] = ()   # fields copied into metadata when created
    create_action: str = "create"   # "upload" for file rows
    self_actor: bool = False        # anonymous create => the new object IS the actor (registration)
    describe: Optional[Callable] = None  # (instance, kind, diff) -> dict | False | None


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _get(spec, instance, default=""):
    if spec is None:
        return default
    try:
        if callable(spec):
            value = spec(instance)
        else:
            value = instance
            for part in spec.split("."):
                value = getattr(value, part)
        return default if value is None else value
    except Exception:
        return default


def _object_repr(cfg, instance):
    value = _get(cfg.repr, instance, "") if cfg.repr else ""
    if not value:
        try:
            value = str(instance)
        except Exception:
            value = f"#{instance.pk}"
    return clip(value, 255)


def _snapshot(instance, cfg, names=None):
    """{field_name: normalized value} for the fields we care about."""
    data = {}
    for field in instance._meta.concrete_fields:
        if field.name in IGNORED_FIELDS or field.name in cfg.exclude:
            continue
        if cfg.only and field.name not in cfg.only:
            continue
        if names is not None and field.name not in names and field.attname not in names:
            continue
        value = getattr(instance, field.attname, None)
        if hasattr(value, "storage") and hasattr(value, "name"):  # FieldFile -> just the name
            value = value.name or ""
        data[field.name] = normalize(value)
    return data


def _diff(old, new):
    diff = {}
    for key, new_value in new.items():
        old_value = old.get(key)
        if old_value == new_value:
            continue
        if is_sensitive(key):
            diff[key] = {"from": "•••", "to": "•••"}
        else:
            diff[key] = {"from": shorten(old_value), "to": shorten(new_value)}
    return diff


def _summary(instance, cfg):
    out = {}
    for name in cfg.summary:
        if is_sensitive(name):
            continue
        try:
            value = getattr(instance, name)
        except Exception:
            continue
        if hasattr(value, "storage") and hasattr(value, "name"):
            value = value.name or ""
        out[name] = shorten(normalize(value))
    return out


def _nice(field_name):
    return field_name.replace("_", " ")


def _action_for_update(diff):
    status = diff.get("status")
    if status:
        new_status = str(status.get("to", "")).lower()
        if new_status in APPROVE_WORDS:
            return "approve"
        if new_status in REJECT_WORDS:
            return "reject"
    return "update"


def _sentence(cfg, action, obj_repr, diff=None):
    label = cfg.label
    if action == "create":
        return f"Created {label} “{obj_repr}”"
    if action == "upload":
        return f"Uploaded {label} “{obj_repr}”"
    if action == "delete":
        return f"Deleted {label} “{obj_repr}”"
    if action == "approve":
        return f"Approved {label} “{obj_repr}”"
    if action == "reject":
        return f"Rejected {label} “{obj_repr}”"
    diff = diff or {}
    if "status" in diff:
        return f"Updated {label} “{obj_repr}”: status {diff['status']['from']} → {diff['status']['to']}"
    fields = [_nice(name) for name in diff]
    shown = ", ".join(fields[:5]) + (f" +{len(fields) - 5} more" if len(fields) > 5 else "")
    return f"Updated {label} “{obj_repr}” ({shown})"


def _model_key(model):
    return f"{model._meta.app_label}.{model._meta.object_name}"


# --------------------------------------------------------------------------
# special-case describers (messages / calls) — these are private, so we
# record THAT it happened and with whom, never the message text.
# --------------------------------------------------------------------------


def _describe_message(instance, kind, diff):
    try:
        other = user_label(instance.recipient) if instance.recipient_id else ""
    except Exception:
        other = ""
    other = other or "a conversation"
    if kind == "create":
        meta = {"kind": instance.kind, "conversationId": instance.conversation_id}
        if instance.attachment:
            meta["attachment"] = instance.attachment_name
        return {
            "action": "create",
            "description": f"Sent a {instance.kind} message to {other}",
            "object_repr": other,
            "metadata": meta,
        }
    if kind == "update" and diff and diff.get("is_deleted", {}).get("to") is True:
        return {"action": "delete", "description": f"Deleted a message to {other}", "object_repr": other}
    return False  # read receipts, hard cascade deletes, etc. — not worth a row


def _describe_call(instance, kind, diff):
    try:
        other = user_label(instance.callee)
    except Exception:
        other = ""
    other = other or "someone"
    if kind == "create":
        return {
            "action": "create",
            "description": f"Started {'an' if str(instance.call_type)[:1].lower() in 'aeiou' else 'a'} {instance.call_type} call with {other}",
            "object_repr": other,
            "metadata": {"callType": instance.call_type},
        }
    if kind == "update" and diff and "status" in diff:
        return {
            "action": "update",
            "description": f"Call with {other} {diff['status']['to']}",
            "object_repr": other,
        }
    return False


def _describe_daily_report(instance, kind, diff):
    if kind == "delete":
        return {"action": "delete", "description": f"Deleted daily report for {instance.date} by {instance.user_name or 'unknown'}"}
    return None


# --------------------------------------------------------------------------
# what gets recorded
# --------------------------------------------------------------------------

TRACKED = {
    # ---- projects
    "projects.Project": Track("Projects", "project", repr="name", project="name",
                              summary=("status", "priority", "budget", "start_date", "due_date")),
    "projects.Module": Track("Projects", "project module", repr=lambda o: f"{o.project.name} / {o.name}",
                             project="project.name", summary=("status", "priority", "due_date")),
    "projects.ModuleFile": Track("Projects", "module file", repr="original_name", project="module.project.name",
                                 events=("create", "delete"), create_action="upload"),
    # ---- tasks
    "tasks.Task": Track("Tasks", "task", repr="title", project="project",
                        summary=("status", "priority", "due_date", "assignees")),
    "tasks.TaskZipFile": Track("Tasks", "task zip file", repr="original_name", project="task.project",
                               events=("create", "delete"), create_action="upload"),
    # ---- clients / billing (dashboard app)
    "dashboard.Client": Track("Clients", "client", repr="name", summary=("status", "industry", "country")),
    "dashboard.Order": Track("Clients", "client order", repr=lambda o: f"{o.client.name} — {o.amount}",
                             summary=("amount", "status")),
    "dashboard.Invoice": Track("Clients", "invoice", repr=lambda o: str(o), project="project.name",
                               summary=("amount", "status")),
    "dashboard.ModuleRequest": Track("Clients", "module request", repr=lambda o: str(o), summary=("status",)),
    "dashboard.IntakeRequest": Track("Clients", "project request",
                                     repr=lambda o: o.company_name or o.contact_person or o.email,
                                     summary=("project_name", "budget", "status")),
    # ---- money
    "dashboard.Expense": Track("Expenses", "expense", repr="title", summary=("amount", "category", "date")),
    "dashboard.Income": Track("Income", "income entry", repr="description", project="project",
                              summary=("amount", "status", "method", "client")),
    "expenses.Expense": Track("Expenses", "expense", repr="title", project="project",
                              summary=("amount", "category", "status", "date")),
    "sales.Sale": Track("Sales", "sale", repr=lambda o: f"{o.client} — {o.project}", project="project",
                        summary=("amount", "status", "date")),
    # ---- meetings
    "meetings.Meeting": Track("Meetings", "meeting", repr="title", project="project",
                              summary=("type", "status", "raw_date", "raw_time")),
    "meetings.MeetingRequest": Track("Meetings", "meeting request", repr=lambda o: f"{o.name} — {o.project}",
                                     project="project", summary=("status", "raw_date")),
    "meetings.RescheduleRequest": Track("Meetings", "reschedule request",
                                        repr=lambda o: f"{o.name} — {o.meeting.title}", project="meeting.project",
                                        summary=("status", "raw_date")),
    # singleton, get_or_create'd on first read => only real edits are interesting
    "meetings.Availability": Track("Meetings", "availability", repr=lambda o: "Shared availability",
                                   events=("update",)),
    # ---- employees
    "employees.EmployeeExtra": Track("Employees", "employee record", repr=lambda o: user_label(o.user),
                                     events=("update",)),
    "employees.LeaveRequest": Track("Employees", "leave request", repr=lambda o: f"{user_label(o.employee)} ({o.type})",
                                    summary=("type", "start_date", "end_date", "days", "status")),
    "employees.Holiday": Track("Employees", "holiday", repr=lambda o: f"{o.date} — {o.reason}"),
    "employees.Announcement": Track("Employees", "announcement",
                                    repr=lambda o: f"{o.type}" + (f" — {user_label(o.employee)}" if o.employee_id else ""),
                                    summary=("type", "detail")),
    # ---- messages (metadata only, never the text)
    "messaging.Message": Track("Messages", "message", events=("create", "update"), only=("is_deleted",),
                               describe=_describe_message),
    "messaging.Call": Track("Messages", "call", events=("create", "update"), only=("status",),
                            describe=_describe_call),
    # ---- users & access
    "users.User": Track("Users", "user", repr=user_label, self_actor=True,
                        summary=("role", "status", "department")),
    "users.Profile": Track("Users", "profile", repr=lambda o: user_label(o.user)),
    "users.UserAccessOverride": Track("Users", "page access override",
                                      repr=lambda o: f"{user_label(o.user)} → {o.mode}", summary=("mode",)),
    "users.UserSubPageAccess": Track("Users", "sub-page access", repr=lambda o: f"{user_label(o.user)} / {o.page}",
                                     summary=("mode",)),
    "users.RolePermission": Track("Users", "role page permissions", repr="role", summary=("pages",)),
    "users.ModulePermission": Track("Users", "module permission", repr=lambda o: f"{o.role} / {o.module}"),
    # ---- settings (singletons => updates only)
    "settings.CompanySettings": Track("Settings", "company settings", repr=lambda o: o.name or "Company",
                                      events=("update",)),
    "settings.SecuritySetting": Track("Settings", "security setting", repr=lambda o: user_label(o.user),
                                      events=("update",)),
    "settings.ProjectSettings": Track("Settings", "project settings", repr=lambda o: "Projects", events=("update",)),
    "settings.TaskSettings": Track("Settings", "task settings", repr=lambda o: "Tasks", events=("update",)),
    "settings.IncomeSettings": Track("Settings", "income settings", repr=lambda o: "Income", events=("update",)),
    "settings.ExpenseSettings": Track("Settings", "expense settings", repr=lambda o: "Expenses", events=("update",)),
    "settings.SalesSettings": Track("Settings", "sales settings", repr=lambda o: "Sales", events=("update",)),
    # ---- reports (the daily-report CREATE is logged by the view, once its files are attached)
    "reports.DailyReport": Track("Reports", "daily report", repr=lambda o: f"{o.date} — {o.project or 'No project'}",
                                 project="project", events=("update", "delete"), describe=_describe_daily_report),
    "reports.CustomReport": Track("Reports", "custom report", repr="name", summary=("category", "format")),
    "reports.ReportOverride": Track("Reports", "report", repr=lambda o: o.custom_name or o.key,
                                    events=("create", "update")),
}

REGISTRY = {}  # model class -> Track


# --------------------------------------------------------------------------
# handlers
# --------------------------------------------------------------------------


def _pre_save(sender, instance, raw=False, **kwargs):
    cfg = REGISTRY.get(sender)
    if cfg is None or raw or "update" not in cfg.events:
        return
    if instance._state.adding or instance.pk is None:
        return
    try:
        old = sender._default_manager.filter(pk=instance.pk).first()
        instance._activity_old = _snapshot(old, cfg) if old is not None else None
    except Exception:
        instance._activity_old = None


def _post_save(sender, instance, created, raw=False, update_fields=None, **kwargs):
    cfg = REGISTRY.get(sender)
    if cfg is None or raw:
        return
    try:
        state = get_state()
        key = _model_key(sender)
        if state is not None:
            state.handled += 1  # a tracked model was saved: the fallback logger must stay out of it

        if created:
            if state is not None:
                state.created.add((key, instance.pk))
            if "create" not in cfg.events:
                return
            override = cfg.describe(instance, "create", None) if cfg.describe else None
            if override is False:
                return
            override = override or {}
            actor = state.user if state else None
            action = override.get("action", cfg.create_action)
            repr_ = override.get("object_repr") or _object_repr(cfg, instance)
            description = override.get("description")
            if actor is None and cfg.self_actor and state is not None:
                actor, action = instance, "register"
                description = f"Registered account “{repr_}”"
            metadata = override.get("metadata") or _summary(instance, cfg)
            log_activity(
                action=action,
                user=actor,
                module=cfg.module,
                description=description or _sentence(cfg, action, repr_),
                object_type=sender._meta.model_name,
                object_id=str(instance.pk),
                object_repr=repr_,
                project=str(_get(cfg.project, instance, "")),
                metadata=metadata,
            )
            return

        # ---- update
        if "update" not in cfg.events:
            return
        old = getattr(instance, "_activity_old", None)
        instance._activity_old = None
        if old is None:
            return
        names = set(update_fields) if update_fields else None
        new = _snapshot(instance, cfg, names)
        diff = _diff({k: old.get(k) for k in new}, new)
        if not diff:
            return
        if state is not None and (key, instance.pk) in state.created and set(diff) <= {"password"}:
            return  # e.g. set_unusable_password() right after creating the account

        override = cfg.describe(instance, "update", diff) if cfg.describe else None
        if override is False:
            return
        override = override or {}
        actor = state.user if state else None
        if actor is None and cfg.self_actor and state is not None:
            actor = instance  # self-service flows (password reset, OTP, profile edit)
        action = override.get("action") or _action_for_update(diff)
        repr_ = override.get("object_repr") or _object_repr(cfg, instance)
        log_activity(
            action=action,
            user=actor,
            module=cfg.module,
            description=override.get("description") or _sentence(cfg, action, repr_, diff),
            object_type=sender._meta.model_name,
            object_id=str(instance.pk),
            object_repr=repr_,
            project=str(_get(cfg.project, instance, "")),
            changes=diff,
            metadata=override.get("metadata") or {},
        )
    except Exception:
        logger.exception("Activity tracking failed for %s", sender)


def _post_delete(sender, instance, **kwargs):
    cfg = REGISTRY.get(sender)
    if cfg is None:
        return
    state = get_state()
    if state is not None:
        state.handled += 1
    if "delete" not in cfg.events:
        return
    try:
        override = cfg.describe(instance, "delete", None) if cfg.describe else None
        if override is False:
            return
        override = override or {}
        repr_ = override.get("object_repr") or _object_repr(cfg, instance)
        log_activity(
            action="delete",
            module=cfg.module,
            description=override.get("description") or _sentence(cfg, "delete", repr_),
            object_type=sender._meta.model_name,
            object_id=str(instance.pk),
            object_repr=repr_,
            project=str(_get(cfg.project, instance, "")),
        )
    except Exception:
        logger.exception("Activity tracking failed for delete of %s", sender)


def _team_changed(sender, instance, action, reverse=False, pk_set=None, **kwargs):
    """Project.team is a many-to-many, which post_save never sees."""
    if reverse or action not in ("post_add", "post_remove", "post_clear"):
        return
    try:
        from django.contrib.auth import get_user_model

        if action == "post_clear":
            description = f"Cleared the team of project “{instance.name}”"
        else:
            people = get_user_model().objects.filter(pk__in=pk_set or [])
            names = ", ".join(user_label(u) for u in people) or "members"
            verb, prep = ("Added", "to") if action == "post_add" else ("Removed", "from")
            description = f"{verb} {names} {prep} the team of project “{instance.name}”"
        log_activity(
            action="update",
            module="Projects",
            description=description,
            object_type="project",
            object_id=str(instance.pk),
            object_repr=instance.name,
            project=instance.name,
            changes={"team": {"from": None, "to": sorted(pk_set or [])}} if action != "post_clear" else {},
        )
    except Exception:
        logger.exception("Activity tracking failed for project team change")


def _delete_daily_report_file(sender, instance, **kwargs):
    """Remove the bytes from disk when a DailyReportFile row goes away
    (including via cascade from its DailyReport). Runs on commit so a
    rolled-back delete never orphans a row from its file."""
    storage, name = instance.file.storage, instance.file.name
    if name:
        transaction.on_commit(lambda: storage.delete(name))


def connect_all():
    for label, cfg in TRACKED.items():
        try:
            model = apps.get_model(label)
        except LookupError:
            continue  # app/model not installed in this project — skip quietly
        REGISTRY[model] = cfg
        pre_save.connect(_pre_save, sender=model, dispatch_uid=f"reports.pre_save.{label}")
        post_save.connect(_post_save, sender=model, dispatch_uid=f"reports.post_save.{label}")
        post_delete.connect(_post_delete, sender=model, dispatch_uid=f"reports.post_delete.{label}")

    try:
        project = apps.get_model("projects.Project")
        m2m_changed.connect(_team_changed, sender=project.team.through, dispatch_uid="reports.project_team")
    except LookupError:
        pass

    post_delete.connect(
        _delete_daily_report_file, sender=apps.get_model("reports.DailyReportFile"),
        dispatch_uid="reports.daily_file_cleanup",
    )
