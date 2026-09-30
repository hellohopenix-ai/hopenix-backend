"""Birthday notifications (phone + laptop, also when the site is closed).

Runs once a day from 08:00 local time (MEETING_TIME_ZONE, default
Asia/Karachi), driven by the same background loop as the meeting reminders
(meetings/reminders.py). Each birthday is announced once per year
(messaging.BirthdayNotice).

  * Employee birthday -> the birthday person gets "Happy Birthday", everybody
    else on the team (not clients) gets "Today is <name>'s birthday".
  * Client birthday   -> every admin and that client's assigned manager get
    "Today is <client>'s birthday"; if the client has a portal login they get
    a "Happy Birthday" of their own.
"""
import calendar
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.db.models import Q

from .models import BirthdayNotice
from .push_utils import notify_user

logger = logging.getLogger(__name__)

SEND_FROM_HOUR = 8


def _now():
    return datetime.now(ZoneInfo(getattr(settings, "MEETING_TIME_ZONE", "Asia/Karachi")))


def _birthday_q(field, today):
    """Rows whose <field> date falls on today's month/day. A 29 Feb birthday is
    celebrated on 28 Feb in years that have no 29 Feb."""
    q = Q(**{f"{field}__month": today.month, f"{field}__day": today.day})
    if today.month == 2 and today.day == 28 and not calendar.isleap(today.year):
        q |= Q(**{f"{field}__month": 2, f"{field}__day": 29})
    return q


def _claim(kind, ref_id, year):
    """True only for the first caller of the year (safe across workers)."""
    try:
        _, created = BirthdayNotice.objects.get_or_create(kind=kind, ref_id=ref_id, year=year)
        return created
    except IntegrityError:
        return False


def _first(name):
    return (name or "").strip().split(" ")[0] or "there"


def _team_birthdays(today):
    from users.models import Profile

    User = get_user_model()
    profiles = (
        Profile.objects.filter(_birthday_q("dob", today), user__is_active=True, user__status="approved")
        .exclude(user__role="client")
        .select_related("user")
    )
    for profile in profiles:
        person = profile.user
        if not _claim("user", person.id, today.year):
            continue
        name = person.name or person.email
        notify_user(person, {
            "type": "birthday.self",
            "title": f"🎂 Happy Birthday, {_first(person.name)}!",
            "body": "Everyone at Hopenix wishes you a wonderful day.",
        })
        colleagues = (
            User.objects.filter(is_active=True, status="approved").exclude(role="client").exclude(id=person.id)
        )
        for colleague in colleagues:
            try:
                notify_user(colleague, {
                    "type": "birthday.team",
                    "userId": person.id,
                    "title": "🎉 Birthday today",
                    "body": f"Today is {name}'s birthday — send your wishes!",
                })
            except Exception:  # noqa: BLE001
                logger.exception("Birthday notify failed for user %s", colleague.id)


def _client_birthdays(today):
    from dashboard.models import Client

    User = get_user_model()
    clients = Client.objects.filter(_birthday_q("date_of_birth", today), status="active").select_related(
        "manager", "portal_user"
    )
    for client in clients:
        if not _claim("client", client.id, today.year):
            continue
        who = client.contact_person or client.name
        label = f"{who} ({client.name})" if client.contact_person and client.name else who

        recipients = {u.id: u for u in User.objects.filter(role="admin", is_active=True)}
        if client.manager_id and client.manager.is_active:
            recipients[client.manager_id] = client.manager
        for user in recipients.values():
            try:
                notify_user(user, {
                    "type": "birthday.client",
                    "clientId": client.id,
                    "title": "🎂 Client birthday today",
                    "body": f"Today is {label}'s birthday — don't forget to wish them!",
                })
            except Exception:  # noqa: BLE001
                logger.exception("Client birthday notify failed for user %s", user.id)

        portal = client.portal_user
        if portal and portal.is_active:
            try:
                notify_user(portal, {
                    "type": "birthday.self",
                    "title": f"🎂 Happy Birthday, {_first(who)}!",
                    "body": "Warm wishes from all of us at Hopenix.",
                })
            except Exception:  # noqa: BLE001
                logger.exception("Client portal birthday notify failed")


def send_birthday_notifications():
    """One pass; cheap when nothing is due. Called every minute by the loop."""
    now = _now()
    if now.hour < SEND_FROM_HOUR:
        return
    today = now.date()
    _team_birthdays(today)
    _client_birthdays(today)
