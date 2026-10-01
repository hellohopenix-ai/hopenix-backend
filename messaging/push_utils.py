"""Sends OS-level browser push notifications via the Web Push protocol
(VAPID), so a call can still "ring" a user even when the Hopenix tab or
browser itself is completely closed and there is no live websocket
connection to push over (see consumers.is_user_online / views.push_to_user
for the "tab is open" path — this file is the "tab is closed" path).

Requires:  pip install pywebpush --break-system-packages
Requires VAPID_PUBLIC_KEY / VAPID_PRIVATE_KEY / VAPID_CLAIMS in settings.py
(see hopenix/settings.py — generate a keypair with
`npx web-push generate-vapid-keys` and paste the values in).

CHANGES in this version
  * The HTTP call to the push service (Google FCM etc.) now runs on a small
    background thread pool. Before, it ran INSIDE the request that sent the
    message / assigned the task, so a slow push service made the sender wait
    up to 10 s per device.
  * send_web_push() now RETURNS a summary (how many devices, how many
    accepted, which errors) instead of nothing, and describe_push_setup()
    reports the server-side state. The new /api/messages/push/test/
    endpoint uses both so "why don't I get notifications" can be answered
    from the app instead of from server logs.
  * Task assignment matches people by name case-insensitively and ignoring
    stray spaces ("ali khan " == "Ali Khan").
"""
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

from django.conf import settings
from django.core.cache import cache
from django.db import close_old_connections

try:
    from pywebpush import WebPushException, webpush
except ImportError:
    WebPushException = Exception
    webpush = None

from .models import PushSubscription

logger = logging.getLogger(__name__)

# A few threads are plenty: each job is just one HTTPS POST per device.
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="webpush")

# When the push service answers 404/410 the browser-side registration is dead.
# The row is deleted, but the browser itself may keep handing the SAME dead
# endpoint back on every app start — which the app would happily save again,
# and push would stay broken forever. So dead endpoints are remembered for a
# while and PushSubscribeView answers 409 "stale" for them; the frontend
# (pushSubscription.js) then drops its old subscription and creates a fresh one.
DEAD_ENDPOINT_TTL = 60 * 60 * 24 * 30


def dead_endpoint_key(endpoint: str) -> str:
    import hashlib

    return "push-dead:" + hashlib.sha1((endpoint or "").encode("utf-8")).hexdigest()


def _device_label(endpoint: str) -> str:
    """Human name of the push service behind a subscription endpoint, only
    used in diagnostics / logs (never sent anywhere)."""
    host = urlparse(endpoint or "").netloc.lower()
    if "googleapis.com" in host:
        return "Chrome (Android / laptop)"
    if "mozilla" in host:
        return "Firefox"
    if "apple.com" in host:
        return "Safari / iPhone"
    if "notify.windows.com" in host:
        return "Edge (Windows)"
    return host or "unknown device"


def _for_recipient(user, payload: dict) -> dict:
    """Client Portal users must land in the PORTAL when they tap a
    notification, not in the staff /dashboard. The service worker (sw.js)
    opens payload["url"] when it is present; staff payloads keep using the
    built-in per-type routing there."""
    if getattr(user, "role", "") != "client" or payload.get("url"):
        return payload
    kind = str(payload.get("type") or "")
    if kind.startswith("project."):
        view = "projects"
    elif kind == "test" or kind.startswith("birthday."):
        view = ""
    else:  # chat messages, calls, thread replies
        view = "messages"
    return {**payload, "url": "/client-portal" + (f"?view={view}" if view else "")}


def _config_problem() -> str:
    """'' when the server can send pushes, else a plain-English reason."""
    if webpush is None:
        return "The pywebpush package is not installed on the server (pip install pywebpush)."
    if not getattr(settings, "VAPID_PRIVATE_KEY", ""):
        return "VAPID_PRIVATE_KEY is empty on the server. Set VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY in the server (Railway) variables."
    if not getattr(settings, "VAPID_PUBLIC_KEY", ""):
        return "VAPID_PUBLIC_KEY is empty on the server. Set it in the server (Railway) variables."
    return ""


def describe_push_setup(user) -> dict:
    """Server-side state for one user, for the in-app notification check."""
    problem = _config_problem()
    subs = list(PushSubscription.objects.filter(user=user).order_by("-created_at"))
    return {
        "configured": not problem,
        "reason": problem,
        "subscriptions": len(subs),
        "devices": [
            {"id": s.id, "device": _device_label(s.endpoint), "since": s.created_at.isoformat()}
            for s in subs
        ],
    }


def _deliver(subs, payload: dict, email: str = "") -> dict:
    """Blocking: POSTs `payload` to every subscription in `subs`. Deletes
    subscriptions the push service says are gone (404 / 410). Never raises."""
    # Calls are only worth ringing for a short while; everything else may wait.
    ttl = 45 if payload.get("type") == "call.incoming" else 86400
    data = json.dumps(payload)
    out = {"delivered": 0, "removed": 0, "errors": []}

    for sub in subs:
        label = _device_label(sub.endpoint)
        try:
            webpush(
                subscription_info={
                    "endpoint": sub.endpoint,
                    "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
                },
                data=data,
                vapid_private_key=settings.VAPID_PRIVATE_KEY,
                vapid_claims=dict(settings.VAPID_CLAIMS),  # webpush mutates this dict — always pass a fresh copy
                ttl=ttl,
                # "high" makes Android deliver straight away even in battery
                # saving (Doze) instead of batching the push for later.
                headers={"Urgency": "high"},
                timeout=10,  # never let a slow push service hang for long
            )
            out["delivered"] += 1
        except WebPushException as exc:
            response = getattr(exc, "response", None)
            status_code = getattr(response, "status_code", None)
            if status_code in (404, 410):
                try:
                    cache.set(dead_endpoint_key(sub.endpoint), 1, DEAD_ENDPOINT_TTL)
                except Exception:  # noqa: BLE001 - cache trouble must not stop the cleanup
                    logger.exception("Could not remember dead push endpoint")
                try:
                    sub.delete()
                except Exception:  # noqa: BLE001
                    logger.exception("Could not delete expired push subscription %s", sub.pk)
                out["removed"] += 1
                logger.info("Web push: removed expired subscription of %s (%s).", email, status_code)
            else:
                body = ""
                try:
                    body = (response.text or "")[:300] if response is not None else ""
                except Exception:  # noqa: BLE001
                    pass
                out["errors"].append({"device": label, "status": status_code, "detail": body or str(exc)[:200]})
                logger.error("Web push failed for %s: status=%s error=%s body=%s", email, status_code, exc, body)
        except Exception as exc:  # noqa: BLE001 - bad key format, network error, ...
            out["errors"].append({"device": label, "status": None, "detail": f"{type(exc).__name__}: {str(exc)[:200]}"})
            logger.exception("Web push crashed for %s", email)
    return out


def _deliver_in_background(subs, payload: dict, email: str) -> None:
    try:
        _deliver(subs, payload, email)
    except Exception:  # noqa: BLE001
        logger.exception("Background web push crashed for %s", email)
    finally:
        close_old_connections()  # this worker thread owns its own DB connection


def send_web_push(user, payload: dict, wait: bool = False) -> dict:
    """Pushes `payload` (JSON-serializable) to every device/browser `user`
    has subscribed on. Silently drops subscriptions the browser has since
    revoked (410 Gone / 404 Not Found) by deleting them, so dead endpoints
    don't pile up. Never raises — a push failure should never break the
    call/message flow that triggered it (the websocket/REST path still works).

    By default the network part runs on a background thread and this returns
    immediately (`queued: True`). `wait=True` (used by the test endpoint)
    sends right now and returns the per-device outcome.

    Every failure is written to the server log with the push service's own
    answer, so "why didn't the notification arrive" can be read straight from
    the Railway logs.

    Returns {configured, reason, subscriptions, delivered, removed, errors[, queued]}."""
    result = {"configured": True, "reason": "", "subscriptions": 0, "delivered": 0, "removed": 0, "errors": []}
    payload = _for_recipient(user, payload)

    problem = _config_problem()
    if problem:
        logger.error("Web push NOT sent: %s", problem)
        result.update(configured=False, reason=problem)
        return result

    subs = list(PushSubscription.objects.filter(user=user))
    result["subscriptions"] = len(subs)
    if not subs:
        logger.info("Web push skipped for %s: no subscribed device (Enable was never accepted on a phone/browser).", user.email)
        return result

    if wait or getattr(settings, "WEBPUSH_SYNC", False):
        result.update(_deliver(subs, payload, user.email))
        return result

    _executor.submit(_deliver_in_background, subs, payload, user.email)
    result["queued"] = True
    return result


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


def send_missed_call_push(callee, call_data, caller_name):
    """Shown on the callee's phone/laptop when a call they never picked up
    ends (caller hung up or gave up ringing). Uses the SAME notification tag
    as the ringing one (call-<id>, see public/sw.js) so it REPLACES the
    "Incoming call" banner with a "Missed call" one, icon included."""
    kind = "Video call" if call_data.get("callType") == "video" else "Voice call"
    send_web_push(callee, {
        "type": "call.missed",
        "call": call_data,
        "callerId": call_data.get("callerId"),
        "title": "Missed call",
        "body": f"{caller_name} · {kind}",
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

    payload = {
        "type": "message.new",
        "senderId": message_data.get("senderId"),
        "conversationId": message_data.get("conversation_id"),
        "title": sender_name,
        "body": body,
    }
    # Settings -> Notifications -> "New Message": honour this user's own
    # Push / Email toggles (previously they were saved but never read).
    from settings.notify import send_event_email, wants

    if wants(recipient, "new_message", "push"):
        send_web_push(recipient, payload)
    if wants(recipient, "new_message", "email"):
        # At most one email per sender per 15 min, so a chatty conversation
        # doesn't flood an inbox while the recipient is offline.
        key = f"msg-email:{recipient.id}:{payload['senderId']}"
        if cache.add(key, 1, 15 * 60):
            send_event_email(recipient, "new_message", f"New message from {sender_name}", f"{sender_name}: {body}")


def push_in_app(user, payload: dict) -> bool:
    """Instant in-app path (websocket -> sidebar red dot). Never raises."""
    from .views import push_to_user  # lazy: views.py imports this module

    try:
        push_to_user(user.id, payload)
        return True
    except Exception:  # noqa: BLE001
        logger.exception("Websocket notify failed for user %s", getattr(user, "id", None))
        return False


def notify_user(user, payload: dict, event_key=None, email_subject=None, email_body=None):
    """One call = every delivery path for a single notification:

    1. websocket (push_in_app) — instant, lights the sidebar red dot if
       the app is open in any tab/device of that user;
    2. Web Push (send_web_push) — the OS-level "WhatsApp-style" banner on
       phone/laptop, also when the site is closed. The service worker
       (public/sw.js) skips showing it when the app is already open and
       focused, so nobody gets a banner for something they are looking at;
    3. email — if the user turned Email on for this event.

    Channels 2 and 3 obey the user's Settings -> Notifications toggles
    (see settings/notify.py). Events that are not one of the six toggles
    (calls, birthdays, client-portal updates...) are always delivered as before.
    Never raises — a failed notification must never break the request
    (task save, module edit, ...) that triggered it."""
    from settings.notify import deliver

    try:
        return deliver(user, payload, event_key, email_subject=email_subject, email_body=email_body)
    except Exception:  # noqa: BLE001
        logger.exception("Notify failed for user %s", getattr(user, "id", None))
        return {"inapp": False, "push": False, "email": False}


def notify_client_portal(client, payload: dict):
    """Notify a Client (dashboard.Client) on their portal login, if they have
    one and it is active. Same two delivery paths as notify_user, so the
    client gets the OS-level banner even when the portal is closed."""
    portal = getattr(client, "portal_user", None)
    if portal is None or not portal.is_active:
        return
    notify_user(portal, payload)


def notify_client_thread_reply(message, staff_user):
    """Staff replied in the client's Messages thread (dashboard.ClientMessage)."""
    text = (getattr(message, "text", "") or "").strip()
    if not text:
        text = "Sent you an attachment" if getattr(message, "attachment", None) else "New message"
    who = (getattr(staff_user, "name", "") or "").strip() or "Your Hopenix team"
    notify_client_portal(message.client, {
        "type": "client.message",
        "title": f"New message from {who}",
        "body": text if len(text) <= 140 else text[:137] + "...",
        "tag": f"client-thread-{message.client_id}",
    })


def notify_module_file_approved(module_file):
    """A file was approved onto the Client Portal (projects ModuleViewSet.approve)
    — tell that project's client there is something new to look at."""
    module = module_file.module
    project = module.project
    if not project.client_id:
        return
    notify_client_portal(project.client, {
        "type": "project.update",
        "title": f"Update on {project.name}",
        "body": f'New file for "{module.name}" is ready for you to view.',
        "tag": f"project-update-{project.id}",
    })


def notify_tasks_assigned(assignments, assigner):
    """`assignments` = iterable of (task, [display names newly added to it]).

    Task.assignees stores display NAMES (not user ids), so people are
    matched by User.name — the same matching the ?assignee= filter and the
    Tasks page already use — but case-insensitively and ignoring stray
    spaces, so "ali khan" still finds "Ali Khan". One notification per
    person even when several tasks were assigned in one go (bulk
    role-template create), and never to the person who made the assignment."""
    from django.contrib.auth import get_user_model
    from django.db.models.functions import Lower, Trim

    def norm(name):
        return " ".join(str(name or "").split()).lower()

    per_key = {}
    for task, names in assignments:
        for name in names:
            key = norm(name)
            if key and task not in per_key.setdefault(key, []):
                per_key[key].append(task)
    if not per_key:
        return

    assigner_id = getattr(assigner, "id", None)
    assigner_name = getattr(assigner, "name", "") or "Someone"
    candidates = (
        get_user_model()
        .objects.filter(is_active=True)
        .annotate(_norm_name=Lower(Trim("name")))
        .filter(_norm_name__in=list(per_key))
    )
    notified = set()
    for user in candidates:
        if user.id == assigner_id or user.id in notified:
            continue
        notified.add(user.id)
        tasks = per_key.get(norm(user.name)) or []
        if not tasks:
            continue
        single = len(tasks) == 1
        body = (
            f'{assigner_name} assigned you "{tasks[0].title}"'
            if single
            else f"{assigner_name} assigned you {len(tasks)} tasks"
        )
        notify_user(user, {
            "type": "task.assigned",
            "taskId": tasks[0].id if single else None,
            "title": "New task assigned",
            "body": body,
        }, email_subject="New task assigned", email_body=body)


def notify_module_assigned(module, assigner):
    """A project module (which also shows up as a task) got `module.assignee`
    as its new assignee. No-op for self-assignment / no assignee."""
    assignee = getattr(module, "assignee", None)
    if assignee is None or assignee.id == getattr(assigner, "id", None):
        return
    assigner_name = getattr(assigner, "name", "") or "Someone"
    project_name = getattr(module.project, "name", "") or "a project"
    body = f'{assigner_name} assigned you "{module.name}" in {project_name}'
    notify_user(assignee, {
        "type": "task.assigned",
        "taskId": None,
        "title": "New task assigned",
        "body": body,
    }, email_subject="New task assigned", email_body=body)


def notify_project_assigned(project, users, assigner):
    """People newly added to a project (as manager or team member) get a
    notification. `users` = iterable of User objects. Never notifies the
    person who made the change."""
    assigner_id = getattr(assigner, "id", None)
    assigner_name = getattr(assigner, "name", "") or "Someone"
    seen = set()
    for user in users:
        if user is None or user.id == assigner_id or user.id in seen or not user.is_active:
            continue
        seen.add(user.id)
        body = f'{assigner_name} added you to "{project.name}"'
        notify_user(user, {
            "type": "project.assigned",
            "projectId": project.id,
            "title": "Added to a project",
            "body": body,
        }, email_subject="Added to a project", email_body=body)


def notify_staff_client_message(client, text, subject=""):
    """A CLIENT wrote in their portal (Messages tab or Support form) — tell
    the admins with a phone/laptop banner, also when Hopenix is closed.
    Never raises: a failed push must never break saving the message."""
    try:
        from settings.notify import admin_users

        text = (text or "").strip() or "Sent a message"
        body = text if len(text) <= 140 else text[:137] + "..."
        title = f"{client.name}: {subject}" if subject else (client.name or "Client message")
        for admin in admin_users():
            notify_user(admin, {
                "type": "client.message.in",
                "title": title,
                "body": body,
                "tag": f"client-in-{client.id}",
                "url": "/dashboard?tab=Clients",
            })
    except Exception:  # noqa: BLE001
        logger.exception("Could not notify staff about a client message")