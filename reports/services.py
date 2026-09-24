import datetime
import decimal
import json
import logging
import uuid

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .constants import REDACTED, SENSITIVE_FIELD_HINTS, SENSITIVE_FIELDS
from .context import get_state

logger = logging.getLogger(__name__)


def clip(text, limit):
    text = "" if text is None else str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def normalize(value):
    """Make any model field value JSON-safe WITHOUT shortening it — used to
    compare old vs new so a change hidden past the first N characters of a
    big JSON field is still noticed."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(k): normalize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [normalize(v) for v in value]
    return str(value)


def shorten(value, limit=200):
    """Shorten a normalized value for storage, so a huge JSON blob or a
    base64 data: URL can't bloat the log table."""
    if isinstance(value, str):
        return clip(value, limit)
    if isinstance(value, (list, dict)):
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(value)
        return clip(text, limit)
    return value


def jsonable(value, limit=200):
    return shorten(normalize(value), limit)


def is_sensitive(field_name):
    name = field_name.lower()
    return name in SENSITIVE_FIELDS or any(hint in name for hint in SENSITIVE_FIELD_HINTS)


def user_label(user):
    if not user:
        return ""
    return (getattr(user, "name", "") or getattr(user, "email", "") or str(user)).strip()


def client_ip(request):
    """REMOTE_ADDR by default. X-Forwarded-For is only honoured when
    ACTIVITY_LOG_TRUST_PROXY_HEADERS = True (i.e. you're behind a proxy that
    sets it) — otherwise any client could spoof its logged IP."""
    if request is None:
        return None
    if getattr(settings, "ACTIVITY_LOG_TRUST_PROXY_HEADERS", False):
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        if forwarded:
            return forwarded.split(",")[0].strip() or None
    return request.META.get("REMOTE_ADDR") or None


def log_activity(
    *,
    action,
    user=None,
    module="General",
    description="",
    object_type="",
    object_id="",
    object_repr="",
    project="",
    changes=None,
    metadata=None,
    actor_name=None,
    actor_email=None,
    actor_role=None,
    dedupe_seconds=0,
    status_code=None,
):
    """Record one activity row. NEVER raises — a logging problem must not be
    able to break login, saving a task, or anything else the user is doing.

    `user` defaults to whoever is behind the current HTTP request. Request
    details (IP, path, method, user agent) are picked up automatically.
    """
    try:
        state = get_state()
        request = state.request if state else None
        if user is None and state is not None:
            user = state.user

        ip = client_ip(request)
        name = actor_name if actor_name is not None else user_label(user)
        email = actor_email if actor_email is not None else (getattr(user, "email", "") or "")
        role = actor_role if actor_role is not None else (getattr(user, "role", "") or "")

        if dedupe_seconds:
            cutoff = timezone.now() - datetime.timedelta(seconds=dedupe_seconds)
            from .models import ActivityLog

            if ActivityLog.objects.filter(
                action=action, actor_email=email, ip_address=ip, created_at__gte=cutoff
            ).exists():
                return None

        from .models import ActivityLog

        with transaction.atomic():  # savepoint: a failed insert can't poison the caller's transaction
            entry = ActivityLog.objects.create(
                user=user if getattr(user, "pk", None) else None,
                actor_name=clip(name, 255),
                actor_email=clip(email, 254),
                actor_role=clip(role, 20),
                action=action,
                module=module,
                object_type=clip(object_type, 60),
                object_id=clip(object_id, 64),
                object_repr=clip(object_repr, 255),
                description=clip(description, 500),
                project=clip(project, 255),
                changes=changes or {},
                metadata=metadata or {},
                method=request.method[:8] if request else "",
                path=clip(request.path, 255) if request else "",
                status_code=status_code,
                ip_address=ip,
                user_agent=clip(request.META.get("HTTP_USER_AGENT", ""), 255) if request else "",
            )
        if state is not None:
            state.logged += 1
        return entry
    except Exception:
        logger.exception("Could not write activity log entry")
        return None


def redact_changes(changes):
    """Belt-and-braces: make sure no sensitive field's value ever reaches the DB."""
    return {
        field: ({"from": REDACTED, "to": REDACTED} if is_sensitive(field) else diff)
        for field, diff in (changes or {}).items()
    }
