"""Birthday notifications (phone + laptop, also when the site is closed).

Sent at 12:00 AM (midnight) on the birthday itself, driven by the same
background loop as the meeting reminders (meetings/reminders.py), so it lands
within a minute of midnight. Each birthday is announced once per year
(messaging.BirthdayNotice).

  * Employees: 12:00 AM Pakistan time (MEETING_TIME_ZONE, default
    Asia/Karachi). The birthday person gets "Happy Birthday"; ONLY the admin(s)
    and that person's own manager are told "Today is <name>'s birthday" - the
    rest of the team is not notified.
  * Clients: 12:00 AM in the client's OWN country (Client.country_code /
    Client.country -> messaging/country_timezones.py); no country -> Pakistan.

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

from .country_timezones import COUNTRY_CODE_BY_NAME, COUNTRY_TIMEZONES, DEFAULT_TIMEZONE
from .models import BirthdayNotice
from .push_utils import notify_user

logger = logging.getLogger(__name__)

# Midnight: the pass runs every minute and each birthday is claimed once, so
# it goes out in the first minute after 12:00 AM (or as soon as the server is
# back up, later that same day, if it was down at midnight).
SEND_FROM_HOUR = 0


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
        # Only the admin(s) and this person's own manager - not the whole team.
        colleagues = User.objects.filter(is_active=True, status="approved").exclude(id=person.id).filter(
            Q(role="admin") | Q(id=person.manager_id)
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


def _client_tz(client):
    """The client's own timezone, from their country (code first, then name)."""
    code = (getattr(client, "country_code", "") or "").strip().upper()
    if code not in COUNTRY_TIMEZONES:
        code = COUNTRY_CODE_BY_NAME.get((getattr(client, "country", "") or "").strip().lower(), "")
    return ZoneInfo(COUNTRY_TIMEZONES.get(code, DEFAULT_TIMEZONE))


def _is_birthday_on(dob, day):
    """dob falls on `day` (29 Feb is celebrated on 28 Feb in non-leap years)."""
    if dob.month == day.month and dob.day == day.day:
        return True
    return dob.month == 2 and dob.day == 29 and day.month == 2 and day.day == 28 and not calendar.isleap(day.year)


def _client_birthdays(_unused_today=None):
    from datetime import timedelta, timezone as dt_timezone

    from dashboard.models import Client

    User = get_user_model()
    # Timezones differ per client, so "today" is worked out per client. Only
    # clients whose birthday is within a day of UTC-today can possibly match
    # (every timezone is within +-1 calendar day of UTC) - keeps this cheap.
    utc_today = datetime.now(dt_timezone.utc).date()
    window = Q()
    for offset in (-1, 0, 1):
        window |= _birthday_q("date_of_birth", utc_today + timedelta(days=offset))
    clients = Client.objects.filter(window, status="active").select_related("manager", "portal_user")
    for client in clients:
        local_now = datetime.now(_client_tz(client))
        if local_now.hour < SEND_FROM_HOUR:
            continue
        local_today = local_now.date()
        if not _is_birthday_on(client.date_of_birth, local_today):
            continue
        if not _claim("client", client.id, local_today.year):
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