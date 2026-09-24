"""Read-side aggregation for the Reports page: the Key Summary cards, the
charts, and the per-user "what did everyone do" report. Everything here is
computed live from real tables — nothing is cached or duplicated."""

from collections import defaultdict

from django.apps import apps
from django.contrib.auth import get_user_model
from django.db.models import Count, Max, Min, Q, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone

from .constants import MODULE_TO_CATEGORY
from .models import ActivityLog, DailyReport
from .utils import fill_days

User = get_user_model()

CURRENCY_SYMBOLS = {"USD": "$", "PKR": "Rs ", "EUR": "€", "GBP": "£", "AED": "AED ", "SAR": "SAR "}


def currency_symbol():
    """Company currency from Settings -> General, without creating the
    singleton row as a side effect of merely opening the Reports page."""
    try:
        row = apps.get_model("settings", "CompanySettings").objects.first()
        code = (row.currency if row else "") or "USD"
    except Exception:
        code = "USD"
    return CURRENCY_SYMBOLS.get(code.upper(), f"{code} ")


def _f(value):
    return float(value or 0)


def real_employees():
    """Same definition EmployeesPage.jsx / ReportsPage.jsx use: approved
    accounts that aren't admins or clients."""
    return User.objects.filter(status="approved").exclude(role__in=["admin", "client"])


def entity_stats():
    Project = apps.get_model("projects", "Project")
    Task = apps.get_model("tasks", "Task")
    Client = apps.get_model("dashboard", "Client")

    today = timezone.localdate()
    projects = Project.objects.filter(is_archived=False)
    totals = projects.aggregate(budget=Sum("budget"), spent=Sum("spent"))
    total_budget, total_spent = _f(totals["budget"]), _f(totals["spent"])

    tasks = Task.objects.all()
    total_tasks = tasks.count()
    completed_tasks = tasks.filter(status="Completed").count()
    overdue_tasks = tasks.exclude(status="Completed").filter(Q(status="Overdue") | Q(due_date__lt=today)).count()

    return {
        "totalEmployees": real_employees().count(),
        "totalProjects": projects.count(),
        "activeProjects": projects.filter(status="In Progress").count(),
        "completedProjects": projects.filter(status="Completed").count(),
        "totalTasks": total_tasks,
        "completedTasks": completed_tasks,
        "overdueTasks": overdue_tasks,
        "totalBudget": total_budget,
        "totalSpent": total_spent,
        "netRemaining": total_budget - total_spent,
        "activeClients": Client.objects.filter(status="active").count(),
    }


def finance_stats():
    Sale = apps.get_model("sales", "Sale")
    Income = apps.get_model("dashboard", "Income")
    Expense = apps.get_model("expenses", "Expense")
    sales = Sale.objects.aggregate(total=Sum("amount"), n=Count("id"))
    paid = Sale.objects.filter(status="Paid").aggregate(total=Sum("amount"))["total"]
    income = Income.objects.filter(status="Received").aggregate(total=Sum("amount"), n=Count("id"))
    expenses = Expense.objects.filter(status="Approved").aggregate(total=Sum("amount"), n=Count("id"))
    return {
        "salesCount": sales["n"] or 0,
        "salesTotal": _f(sales["total"]),
        "salesPaid": _f(paid),
        "incomeReceived": _f(income["total"]),
        "incomeCount": income["n"] or 0,
        "expensesApproved": _f(expenses["total"]),
        "expensesCount": expenses["n"] or 0,
    }


def chart_data():
    Project = apps.get_model("projects", "Project")
    projects = list(Project.objects.filter(is_archived=False).order_by("-budget", "name"))

    by_status = defaultdict(int)
    for p in projects:
        by_status[p.status or "Unknown"] += 1

    return {
        "budgetVsSpent": [{"project": p.name, "budget": _f(p.budget), "spent": _f(p.spent)} for p in projects[:30]],
        "projectsByStatus": [{"name": name, "value": value} for name, value in by_status.items()],
        "topProjectsByBudget": [{"name": p.name, "value": _f(p.budget)} for p in projects if p.budget and p.budget > 0][:5],
    }


def activity_block(logs, rng, tz):
    """Headline numbers + charts data for an ActivityLog queryset already
    scoped to the range."""
    total = logs.count()
    with_user = logs.exclude(user__isnull=True)

    by_module = list(logs.values("module").annotate(count=Count("id")).order_by("-count"))
    by_action = list(logs.values("action").annotate(count=Count("id")).order_by("-count"))

    day_rows = (
        logs.annotate(day=TruncDate("created_at", tzinfo=tz)).values("day").annotate(count=Count("id")).order_by("day")
    )
    by_day = {row["day"]: row["count"] for row in day_rows}

    top = list(with_user.values("user_id").annotate(count=Count("id")).order_by("-count")[:5])
    names = {u.id: (u.name or u.email) for u in User.objects.filter(id__in=[t["user_id"] for t in top])}

    return {
        "totalActions": total,
        "activeUsers": with_user.values("user_id").distinct().count(),
        "byModule": [{"module": r["module"], "category": MODULE_TO_CATEGORY.get(r["module"], "General"), "count": r["count"]} for r in by_module],
        "byAction": [{"action": r["action"], "label": ActivityLog.ACTION_LABELS.get(r["action"], r["action"]), "count": r["count"]} for r in by_action],
        "byDay": fill_days(by_day, rng, tz),
        "topUsers": [{"userId": t["user_id"], "name": names.get(t["user_id"], "Unknown"), "count": t["count"]} for t in top],
    }


def _presence(user):
    return user.presence_status() if hasattr(user, "presence_status") else "Offline"


def user_rows(rng, tz, role="", search="", only_ids=None):
    """One row per user: what they did in the range, broken down by module and
    action, plus how active they've been overall. Users with no activity in
    the range are still listed (with zeros) — "who did nothing" is a
    legitimate thing for a report to show.

    only_ids restricts every aggregate to those users (used by the single-user
    detail report so it doesn't compute everyone's numbers)."""
    base = ActivityLog.objects.exclude(user__isnull=True)
    if only_ids is not None:
        base = base.filter(user_id__in=only_ids)
    logs = rng.apply(base)

    totals = {r["user_id"]: r for r in logs.values("user_id").annotate(total=Count("id"))}
    by_module, by_action = defaultdict(dict), defaultdict(dict)
    for r in logs.values("user_id", "module").annotate(c=Count("id")):
        by_module[r["user_id"]][r["module"]] = r["c"]
    for r in logs.values("user_id", "action").annotate(c=Count("id")):
        by_action[r["user_id"]][r["action"]] = r["c"]
    active_days = defaultdict(int)
    for r in logs.annotate(day=TruncDate("created_at", tzinfo=tz)).values("user_id", "day").annotate(c=Count("id")):
        active_days[r["user_id"]] += 1

    lifetime = {
        r["user_id"]: r
        for r in base.values("user_id").annotate(first=Min("created_at"), last=Max("created_at"))
    }

    daily_qs = DailyReport.objects.exclude(user__isnull=True)
    if only_ids is not None:
        daily_qs = daily_qs.filter(user_id__in=only_ids)
    daily = {
        r["user_id"]: r
        for r in rng.apply_dates(daily_qs)
        .values("user_id")
        .annotate(n=Count("id"), approved=Count("id", filter=Q(status="approved")))
    }

    # approved accounts + anyone who shows up in the log for this range
    users_qs = User.objects.filter(Q(status="approved") | Q(id__in=list(totals)))
    if only_ids is not None:
        users_qs = User.objects.filter(id__in=only_ids)
    if role:
        users_qs = users_qs.filter(role=role)
    if search:
        users_qs = users_qs.filter(Q(name__icontains=search) | Q(email__icontains=search))
    users_qs = users_qs.select_related("profile")

    rows = []
    for u in users_qs:
        t = totals.get(u.id)
        life = lifetime.get(u.id)
        d = daily.get(u.id)
        rows.append(
            {
                "user": u,
                "totalActions": t["total"] if t else 0,
                "byModule": by_module.get(u.id, {}),
                "byAction": by_action.get(u.id, {}),
                "activeDays": active_days.get(u.id, 0),
                "firstActivityAt": life["first"] if life else None,
                "lastActivityAt": life["last"] if life else None,
                "dailyReports": d["n"] if d else 0,
                "dailyReportsApproved": d["approved"] if d else 0,
                "presence": _presence(u),
            }
        )
    rows.sort(key=lambda r: (-r["totalActions"], (r["user"].name or r["user"].email).lower()))
    return rows


def workload(user):
    """What's currently on this person's plate — from the real Task / Project
    tables (Task.assignees is a list of display names, so match on name)."""
    Task = apps.get_model("tasks", "Task")
    Project = apps.get_model("projects", "Project")
    today = timezone.localdate()
    name = (user.name or "").strip().lower()

    mine = [t for t in Task.objects.only("assignees", "status", "due_date") if name and name in [str(a).strip().lower() for a in (t.assignees or [])]]
    done = sum(1 for t in mine if t.status == "Completed")
    overdue = sum(1 for t in mine if t.status != "Completed" and (t.status == "Overdue" or (t.due_date and t.due_date < today)))

    projects = Project.objects.filter(is_archived=False).filter(Q(team=user) | Q(manager=user)).distinct().count()
    return {"tasksAssigned": len(mine), "tasksCompleted": done, "tasksOverdue": overdue, "projects": projects}
