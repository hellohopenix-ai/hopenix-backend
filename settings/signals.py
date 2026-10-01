"""Fires the Settings -> Notifications events that no view had a hook for.

  task_completed  a Task's status changes to "Completed"
  invoice_paid    a client Invoice's status changes to "paid"
  project_update  a Project's status changes (e.g. In Progress -> In Review)

These are model signals (not edits inside the individual views) so every code
path is covered — the "complete" action, a plain PATCH from the Tasks page,
the admin confirming a payment in Clients, etc. Who gets told:

  task_completed / invoice_paid -> the admins (except whoever did it)
  project_update                -> the project's manager + team (except whoever did it)

Whether a person actually receives it over push / email is decided per user by
their own Notifications tab — see settings/notify.py. Every handler swallows its
own errors: a notification problem must never break the save that caused it.
"""
import logging

from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from .notify import admin_users, deliver

logger = logging.getLogger(__name__)


def _actor():
    """The user behind the current HTTP request, if any (reports/context.py)."""
    try:
        from reports.context import _current

        state = _current.get()
        return state.user if state else None
    except Exception:  # noqa: BLE001
        return None


def _remember_status(sender, instance, **kwargs):
    old = None
    if instance.pk:
        old = sender.objects.filter(pk=instance.pk).values_list("status", flat=True).first()
    instance._status_before_save = old


def _status_changed_to(instance, new_value):
    before = getattr(instance, "_status_before_save", None)
    return before is not None and before != new_value and instance.status == new_value


def _send(users, payload, event_key, subject=None):
    actor = _actor()
    actor_id = getattr(actor, "id", None)
    for user in users:
        if user.id == actor_id:
            continue
        deliver(user, payload, event_key, email_subject=subject or payload["title"], email_body=payload["body"])


def _connect():
    from django.apps import apps

    Task = apps.get_model("tasks", "Task")
    Invoice = apps.get_model("dashboard", "Invoice")
    Project = apps.get_model("projects", "Project")

    for model in (Task, Invoice, Project):
        pre_save.connect(_remember_status, sender=model, dispatch_uid=f"settings.remember_status.{model.__name__}")

    @receiver(post_save, sender=Task, dispatch_uid="settings.task_completed")
    def task_completed(sender, instance, created, **kwargs):
        try:
            if created or not _status_changed_to(instance, "Completed"):
                return
            _send(
                admin_users(),
                {
                    "type": "task.completed",
                    "taskId": instance.id,
                    "title": "Task completed",
                    "body": f'"{instance.title}" was marked completed',
                },
                "task_completed",
            )
        except Exception:  # noqa: BLE001
            logger.exception("task_completed notification failed")

    @receiver(post_save, sender=Invoice, dispatch_uid="settings.invoice_paid")
    def invoice_paid(sender, instance, created, **kwargs):
        try:
            if created or not _status_changed_to(instance, "paid"):
                return
            client_name = getattr(instance.client, "name", "") or "a client"
            amount = instance.paid_amount or instance.amount
            _send(
                admin_users(),
                {
                    "type": "invoice.paid",
                    "title": "Invoice paid",
                    "body": f"{client_name} — milestone {instance.milestone_number}/{instance.milestone_total} "
                    f"paid ({amount})",
                },
                "invoice_paid",
            )
        except Exception:  # noqa: BLE001
            logger.exception("invoice_paid notification failed")

    @receiver(post_save, sender=Project, dispatch_uid="settings.project_update")
    def project_update(sender, instance, created, **kwargs):
        try:
            before = getattr(instance, "_status_before_save", None)
            if created or before is None or before == instance.status:
                return
            people = {u.id: u for u in instance.team.filter(is_active=True)}
            if instance.manager_id and instance.manager and instance.manager.is_active:
                people[instance.manager_id] = instance.manager
            _send(
                people.values(),
                {
                    "type": "project.updated",
                    "title": "Project updated",
                    "body": f'"{instance.name}" is now {instance.status}',
                },
                "project_update",
            )
        except Exception:  # noqa: BLE001
            logger.exception("project_update notification failed")


def register():
    _connect()
