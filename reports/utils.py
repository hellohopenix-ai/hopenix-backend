import zoneinfo
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Optional

from django.utils import timezone

PRESETS = ("today", "yesterday", "week", "month", "quarter", "year", "last7", "last30", "all")


class RangeError(ValueError):
    """Bad ?start / ?end / ?range value — the view turns this into a 400."""


@dataclass
class DateRange:
    start: Optional[date]          # first day, inclusive (None = open)
    end: Optional[date]            # last day, inclusive (None = open)
    start_dt: Optional[datetime]   # aware start-of-day, inclusive
    end_dt: Optional[datetime]     # aware start of the day AFTER `end`, exclusive

    def apply(self, qs, field="created_at"):
        if self.start_dt:
            qs = qs.filter(**{f"{field}__gte": self.start_dt})
        if self.end_dt:
            qs = qs.filter(**{f"{field}__lt": self.end_dt})
        return qs

    def apply_dates(self, qs, field="date"):
        """Same range, for a plain DateField (e.g. DailyReport.date)."""
        if self.start:
            qs = qs.filter(**{f"{field}__gte": self.start})
        if self.end:
            qs = qs.filter(**{f"{field}__lte": self.end})
        return qs


def get_tz(request):
    """Browser timezone (?tz=Asia/Karachi) so "today" and day buckets match
    what the person sees; falls back to the server timezone."""
    name = request.query_params.get("tz") if hasattr(request, "query_params") else request.GET.get("tz")
    if name:
        try:
            return zoneinfo.ZoneInfo(name)
        except Exception:
            pass
    return timezone.get_current_timezone()


def _parse_date(value, label):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise RangeError(f"{label} must be a date like 2026-09-20.")


def _preset(name, today):
    if name == "all":
        return None, None
    if name == "today":
        return today, today
    if name == "yesterday":
        y = today - timedelta(days=1)
        return y, y
    if name == "week":
        return today - timedelta(days=today.weekday()), today
    if name == "month":
        return today.replace(day=1), today
    if name == "quarter":
        first_month = 3 * ((today.month - 1) // 3) + 1
        return today.replace(month=first_month, day=1), today
    if name == "year":
        return today.replace(month=1, day=1), today
    if name == "last7":
        return today - timedelta(days=6), today
    if name == "last30":
        return today - timedelta(days=29), today
    raise RangeError(f"range must be one of: {', '.join(PRESETS)}.")


def resolve_range(params, tz, default="last30"):
    """?start=YYYY-MM-DD&end=YYYY-MM-DD wins; otherwise ?range=<preset>."""
    today = datetime.now(tz).date()
    start_s, end_s = params.get("start"), params.get("end")
    if start_s or end_s:
        start = _parse_date(start_s, "start") if start_s else None
        end = _parse_date(end_s, "end") if end_s else None
    else:
        start, end = _preset(params.get("range") or default, today)

    if start and end and start > end:
        raise RangeError("start must not be after end.")

    start_dt = datetime.combine(start, time.min, tzinfo=tz) if start else None
    end_dt = datetime.combine(end + timedelta(days=1), time.min, tzinfo=tz) if end else None
    return DateRange(start, end, start_dt, end_dt)


def fill_days(rows, rng, tz, cap=366):
    """rows: {date: count}. Returns [{"date": "YYYY-MM-DD", "count": n}] with
    zero-filled gaps, capped to the last `cap` days so "all time" can't
    explode the response."""
    if not rows and not (rng.start and rng.end):
        return []
    today = datetime.now(tz).date()
    end = rng.end or today
    start = rng.start or (min(rows) if rows else end)
    if (end - start).days + 1 > cap:
        start = end - timedelta(days=cap - 1)
    out, day = [], start
    while day <= end:
        out.append({"date": day.isoformat(), "count": int(rows.get(day, 0))})
        day += timedelta(days=1)
    return out


def money(n):
    """Whole-number, thousands-separated — mirrors fmtCurrency() in ReportsPage.jsx."""
    return f"{int(round(float(n or 0))):,}"
