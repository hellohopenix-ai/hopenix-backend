"""Makes the Settings -> Notifications tab actually DO something.

Until now that tab only saved rows into NotificationPreference — nothing
that sends a notification ever looked at them. Everything that notifies a
person about one of the six events in the tab goes through here now:

    task_assigned   task_completed   project_update
    invoice_paid    new_message      system_alerts

Channels:
  * in-app  — the live websocket (sidebar red dot). Always delivered; it
              is the app itself telling you something, not a "channel"
              you'd want to switch off.
  * push    — OS/browser Web Push. Sent only if that user's `push` toggle
              for the event is on.
  * email   — sent only if that user's `email` toggle for the event is on
              AND the organisation-wide "Allow Email Notifications"
              switch (General tab) is on.
  * sms     — stored, but there is no SMS provider wired into this
              project, so nothing is sent for it (see send_sms below).

A user who never opened the tab has no rows yet; for them the same
defaults the tab itself shows (DEFAULT_NOTIFICATION_EVENTS) apply, so
behaviour is identical before/after they first open Settings.
"""
import logging
import threading

from django.conf import settings as django_settings
from django.core.mail import send_mail
from django.db.models import Q

logger = logging.getLogger(__name__)

# Seed list for a user's Notifications tab — same labels/flags the tab has
# always shown. `event_key` is what the rest of the backend refers to.
DEFAULT_NOTIFICATION_EVENTS = [
    {"event_key": "task_assigned", "label": "Task Assigned", "email": True, "push": True, "sms": False},
    {"event_key": "task_completed", "label": "Task Completed", "email": True, "push": False, "sms": False},
    {"event_key": "project_update", "label": "Project Update", "email": True, "push": True, "sms": False},
    {"event_key": "invoice_paid", "label": "Invoice Paid", "email": True, "push": True, "sms": True},
    {"event_key": "new_message", "label": "New Message", "email": False, "push": True, "sms": False},
    {"event_key": "system_alerts", "label": "System Alerts", "email": True, "push": True, "sms": True},
]
_DEFAULTS = {row["event_key"]: row for row in DEFAULT_NOTIFICATION_EVENTS}

# payload["type"] (what websocket/service-worker use) -> settings event key
PAYLOAD_TYPE_TO_EVENT = {
    "task.assigned": "task_assigned",
    "task.completed": "task_completed",
    "project.updated": "project_update",
    "project.assigned": "project_update",  # "you were added to a project" follows the Project Update toggle
    "invoice.paid": "invoice_paid",
    "message.new": "new_message",
    "system.alert": "system_alerts",
}


def get_preference(user, event_key):
    """{'email': bool, 'push': bool, 'sms': bool} for this user + event."""
    from .models import NotificationPreference

    default = _DEFAULTS.get(event_key, {"email": False, "push": True, "sms": False})
    row = NotificationPreference.objects.filter(user=user, event_key=event_key).first()
    if row is None:
        return {"email": default["email"], "push": default["push"], "sms": default["sms"]}
    return {"email": row.email, "push": row.push, "sms": row.sms}


def wants(user, event_key, channel):
    """Does `user` want `event_key` delivered over `channel`?"""
    if not event_key:
        return True  # unknown / non-configurable event (e.g. incoming calls): always deliver
    return bool(get_preference(user, event_key).get(channel, False))


def _company_email_enabled():
    from .models import CompanySettings

    return CompanySettings.load().email_notifications


def send_event_email(user, event_key, subject, body):
    """Email `user` about `event_key` if they (and the company) allow it.
    Runs in a background thread so a slow mail provider never delays the
    request (task save, invoice update...) that triggered it. Never raises."""
    try:
        if not getattr(user, "email", ""):
            return False
        if not wants(user, event_key, "email") or not _company_email_enabled():
            return False
    except Exception:  # noqa: BLE001
        logger.exception("Email preference check failed for user %s", getattr(user, "id", None))
        return False

    def _send():
        try:
            send_mail(
                subject,
                body,
                django_settings.DEFAULT_FROM_EMAIL,
                [user.email],
                fail_silently=False,
            )
        except Exception:  # noqa: BLE001
            logger.warning("Notification email to %s failed", user.email, exc_info=True)

    threading.Thread(target=_send, daemon=True).start()
    return True


def send_sms(user, event_key, text):
    """Placeholder: no SMS provider (Twilio etc.) is configured in this
    project, so this only records that an SMS *would* have been sent. Plug a
    provider in here and every event above starts sending SMS with no other
    change."""
    logger.info("SMS not configured — skipped %s for user %s", event_key, getattr(user, "id", None))
    return False


def deliver(user, payload, event_key=None, *, email_subject=None, email_body=None):
    """The one call every notifier uses. Returns which channels fired:
    {'inapp': bool, 'push': bool, 'email': bool}."""
    from messaging.push_utils import push_in_app, send_web_push

    event_key = event_key or PAYLOAD_TYPE_TO_EVENT.get(payload.get("type", ""))
    result = {"inapp": False, "push": False, "email": False}

    result["inapp"] = push_in_app(user, payload)

    try:
        if wants(user, event_key, "push"):
            # send_web_push returns a summary dict; delivery runs on a background
            # thread, so "queued" counts as sent here.
            res = send_web_push(user, payload) or {}
            result["push"] = bool(res.get("queued") or res.get("delivered"))
    except Exception:  # noqa: BLE001
        logger.exception("Web push failed for user %s", getattr(user, "id", None))

    if event_key and email_subject:
        result["email"] = send_event_email(user, event_key, email_subject, email_body or payload.get("body", ""))
    if event_key and wants(user, event_key, "sms"):
        send_sms(user, event_key, payload.get("body", ""))
    return result


def admin_users(exclude_ids=()):
    """Active admins — who gets company-level events (invoice paid, task
    completed, storage alert). Same admin test the settings views use."""
    from django.contrib.auth import get_user_model

    User = get_user_model()
    return User.objects.filter(is_active=True).filter(
        Q(role="admin") | Q(is_staff=True) | Q(is_superuser=True)
    ).exclude(id__in=list(exclude_ids)).distinct()


def is_admin(user):
    """Admin = superuser/staff OR the app-level 'admin' role. (The old checks
    only looked at is_staff, so an admin created through the app — role
    'admin' but never a Django staff user — was refused on every company
    setting with a 403.)"""
    return bool(
        user
        and user.is_authenticated
        and (user.is_staff or user.is_superuser or getattr(user, "role", "") == "admin")
    )
