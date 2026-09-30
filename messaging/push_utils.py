"""Sends OS-level browser push notifications via the Web Push protocol
(VAPID), so a call can still "ring" a user even when the Hopenix tab or
browser itself is completely closed and there is no live websocket
connection to push over (see consumers.is_user_online / views.push_to_user
for the "tab is open" path — this file is the "tab is closed" path).

Requires:  pip install pywebpush --break-system-packages
Requires VAPID_PUBLIC_KEY / VAPID_PRIVATE_KEY / VAPID_CLAIMS in settings.py
(see hopenix/settings.py — generate a keypair with
`npx web-push generate-vapid-keys` and paste the values in).
"""
import json
import logging

from django.conf import settings
try:
    from pywebpush import WebPushException, webpush
except ImportError:
    WebPushException = Exception
    webpush = None

from .models import PushSubscription

logger = logging.getLogger(__name__)


def send_web_push(user, payload: dict):
    """Pushes `payload` (JSON-serializable) to every device/browser `user`
    has subscribed on. Silently drops subscriptions the browser has since
    revoked (410 Gone / 404 Not Found) by deleting them, so dead endpoints
    don't pile up. Never raises — a push failure should never break the
    call flow that triggered it (the websocket/REST path still works)."""
    for sub in PushSubscription.objects.filter(user=user):
        try:
            webpush(
                subscription_info={
                    "endpoint": sub.endpoint,
                    "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
                },
                data=json.dumps(payload),
                vapid_private_key=settings.VAPID_PRIVATE_KEY,
                vapid_claims=dict(settings.VAPID_CLAIMS),  # webpush mutates this dict — always pass a fresh copy
            )
        except WebPushException as exc:
            status_code = getattr(exc.response, "status_code", None)
            if status_code in (404, 410):
                sub.delete()
            else:
                logger.warning("Web push failed for %s: %s", user.email, exc)


def send_incoming_call_push(callee, call_data, caller_name):
    """Convenience wrapper for CallStartView — shapes the payload the
    service worker (frontend/public/sw.js) expects for an incoming-call
    notification."""
    send_web_push(callee, {
        "type": "call.incoming",
        "call": call_data,
        "title": f"Incoming call from {caller_name}",
        "body": "Audio call" if call_data.get("callType") == "audio" else "Video call",
    })


def send_new_message_push(recipient, message_data, sender_name):
    """Convenience wrapper for SendMessageView — shapes the payload the
    service worker expects for a new-message notification, the same way
    send_incoming_call_push does for calls. Only called when the recipient
    isn't currently connected over the live websocket (see is_user_online
    in views.py), so an open, focused chat never gets a redundant OS
    notification on top of the message just appearing.

    The body is a short preview — the message text itself for a text
    message, or a generic "Sent a photo/voice message/file" line for
    anything else, so a peeked lock-screen notification never leaks a
    file's contents, only that one arrived."""
    kind = message_data.get("kind")
    if kind == "image":
        body = "📷 Sent a photo"
    elif kind == "voice":
        body = "🎤 Sent a voice message"
    elif kind == "file":
        name = message_data.get("attachmentName") or "a file"
        body = f"📎 Sent {name}"
    else:
        text = (message_data.get("text") or "").strip()
        body = (text[:120] + "…") if len(text) > 120 else (text or "Sent a message")

    send_web_push(recipient, {
        "type": "message.new",
        "senderId": message_data.get("senderId"),
        "conversationId": message_data.get("conversation_id"),
        "title": sender_name,
        "body": body,
    })


def notify_user(user, payload: dict):
    """One call = both delivery paths for a single notification:

    1. websocket (push_to_user) — instant, lights the sidebar red dot if
       the app is open in any tab/device of that user;
    2. Web Push (send_web_push) — the OS-level "WhatsApp-style" banner on
       phone/laptop, also when the site is closed. The service worker
       (public/sw.js) skips showing it when the app is already open and
       focused, so nobody gets a banner for something they are looking at.

    Never raises — a failed notification must never break the request
    (task save, module edit, ...) that triggered it."""
    from .views import push_to_user  # lazy: views.py imports this module

    try:
        push_to_user(user.id, payload)
    except Exception:  # noqa: BLE001
        logger.exception("Websocket notify failed for user %s", user.id)
    try:
        send_web_push(user, payload)
    except Exception:  # noqa: BLE001
        logger.exception("Web push notify failed for user %s", user.id)


def notify_tasks_assigned(assignments, assigner):
    """`assignments` = iterable of (task, [display names newly added to it]).

    Task.assignees stores display NAMES (not user ids), so people are
    matched by User.name — the same matching the ?assignee= filter and the
    Tasks page already use. One notification per person even when several
    tasks were assigned in one go (bulk role-template create), and never
    to the person who made the assignment."""
    from django.contrib.auth import get_user_model

    per_name = {}
    for task, names in assignments:
        for name in names:
            per_name.setdefault(name, []).append(task)
    if not per_name:
        return

    assigner_id = getattr(assigner, "id", None)
    assigner_name = getattr(assigner, "name", "") or "Someone"
    for user in get_user_model().objects.filter(name__in=list(per_name), is_active=True):
        if user.id == assigner_id:
            continue
        tasks = per_name[user.name]
        single = len(tasks) == 1
        notify_user(user, {
            "type": "task.assigned",
            "taskId": tasks[0].id if single else None,
            "title": "New task assigned",
            "body": (
                f'{assigner_name} assigned you "{tasks[0].title}"'
                if single
                else f"{assigner_name} assigned you {len(tasks)} tasks"
            ),
        })


def notify_module_assigned(module, assigner):
    """A project module (which also shows up as a task) got `module.assignee`
    as its new assignee. No-op for self-assignment / no assignee."""
    assignee = getattr(module, "assignee", None)
    if assignee is None or assignee.id == getattr(assigner, "id", None):
        return
    assigner_name = getattr(assigner, "name", "") or "Someone"
    project_name = getattr(module.project, "name", "") or "a project"
    notify_user(assignee, {
        "type": "task.assigned",
        "taskId": None,
        "title": "New task assigned",
        "body": f'{assigner_name} assigned you "{module.name}" in {project_name}',
    })
