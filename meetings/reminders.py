"""Meeting notifications: "meeting scheduled", "meeting today" (morning of the
day) and "starting in 15 minutes". Every notification goes out both ways
(live websocket + OS Web Push, see messaging/push_utils.notify_user), so it
reaches phones/laptops even when the site is closed.

Meeting times are entered as local wall-clock time, so "today" is computed in
MEETING_TIME_ZONE (default Asia/Karachi), not in the server's UTC.
"""
import logging
import os
import sys
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone

logger = logging.getLogger(__name__)

DAY_REMINDER_HOUR = 8       # "Meeting today" goes out from 08:00 local
SOON_MINUTES = 15           # "starting soon" goes out this long before
INACTIVE = ("Declined", "Cancelled", "Completed")


def _tz():
    return ZoneInfo(getattr(settings, "MEETING_TIME_ZONE", "Asia/Karachi"))


def _pretty(meeting):
    return f"{meeting.raw_date.strftime('%d %b')} at {meeting.raw_time.strftime('%I:%M %p').lstrip('0')}"


def meeting_recipients(meeting, exclude_user_id=None):
    """Participants (matched by display name), the creator, and every admin."""
    User = get_user_model()
    ids = set()
    names = [n for n in (meeting.participants or []) if isinstance(n, str)]
    if names:
        ids.update(User.objects.filter(name__in=names, is_active=True).values_list("id", flat=True))
    if meeting.created_by_user_id:
        ids.add(meeting.created_by_user_id)
    ids.update(User.objects.filter(role="admin", is_active=True).values_list("id", flat=True))
    ids.discard(exclude_user_id)
    return User.objects.filter(id__in=ids, is_active=True)


def _send(meeting, users, title, body, kind):
    from messaging.push_utils import notify_user

    for user in users:
        try:
            notify_user(user, {
                "type": kind,
                "meetingId": meeting.id,
                "title": title,
                "body": body,
            })
        except Exception:  # noqa: BLE001
            logger.exception("Meeting notification failed for user %s", user.id)


def after_meeting_created(meeting, actor):
    """Call right after a Meeting is created. Tells everyone involved (not the
    person who created it) and skips the same-day "meeting today" reminder if
    the meeting is for today anyway."""
    from .models import Meeting

    try:
        _send(
            meeting,
            meeting_recipients(meeting, exclude_user_id=getattr(actor, "id", None)),
            "New meeting scheduled",
            f"{meeting.title} · {_pretty(meeting)}",
            "meeting.scheduled",
        )
        if meeting.raw_date == datetime.now(_tz()).date():
            Meeting.objects.filter(pk=meeting.pk).update(day_reminder_sent=True)
    except Exception:  # noqa: BLE001
        logger.exception("after_meeting_created failed")


def send_due_reminders():
    """One pass. Safe to call from several workers at once: each reminder is
    claimed with a conditional UPDATE, so only one of them sends it."""
    from .models import Meeting

    now = datetime.now(_tz())
    today = now.date()
    qs = Meeting.objects.filter(raw_date=today).exclude(status__in=INACTIVE)
    for m in qs:
        start = datetime.combine(m.raw_date, m.raw_time, tzinfo=_tz())
        minutes_left = (start - now).total_seconds() / 60
        if minutes_left < -5:
            continue  # already started, nothing useful to say

        if not m.day_reminder_sent and now.hour >= DAY_REMINDER_HOUR:
            if Meeting.objects.filter(pk=m.pk, day_reminder_sent=False).update(day_reminder_sent=True):
                _send(m, meeting_recipients(m), "Meeting today",
                      f"{m.title} at {m.raw_time.strftime('%I:%M %p').lstrip('0')}", "meeting.today")

        if not m.soon_reminder_sent and minutes_left <= SOON_MINUTES:
            if Meeting.objects.filter(pk=m.pk, soon_reminder_sent=False).update(soon_reminder_sent=True):
                mins = max(int(round(minutes_left)), 0)
                _send(m, meeting_recipients(m), "Meeting starting soon",
                      f"{m.title} starts in {mins} min" if mins else f"{m.title} is starting now", "meeting.soon")


def _loop():
    time.sleep(20)  # let the app finish booting
    while True:
        try:
            send_due_reminders()
        except Exception:  # noqa: BLE001 - e.g. migration not applied yet; keep looping
            logger.exception("Meeting reminder pass failed")
        try:
            from messaging.birthdays import send_birthday_notifications

            send_birthday_notifications()
        except Exception:  # noqa: BLE001
            logger.exception("Birthday notification pass failed")
        time.sleep(60)


def start_reminder_thread():
    """Started once from MeetingsConfig.ready() when the web server is running.
    No cron / extra Railway service needed. Disable with MEETING_REMINDERS=0."""
    if os.environ.get("MEETING_REMINDERS", "1") == "0":
        return
    argv0 = os.path.basename(sys.argv[0]) if sys.argv else ""
    is_server = argv0.startswith(("daphne", "gunicorn", "uvicorn")) or (
        "runserver" in sys.argv and os.environ.get("RUN_MAIN") == "true"
    )
    if not is_server:
        return
    threading.Thread(target=_loop, name="meeting-reminders", daemon=True).start()