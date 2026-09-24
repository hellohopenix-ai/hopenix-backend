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
