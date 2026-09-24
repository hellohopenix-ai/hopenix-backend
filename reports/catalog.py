"""The rows in ReportsPage.jsx's "All Reports" table.

Same idea the page already had client-side — one row per real employee, per
project and per client, plus a few aggregate reports — but computed here from
the real tables instead of localStorage, so it always matches what is
actually in the system. IDs match the ones the page used (emp-<id>,
proj-<id>, client-<id>, agg-...) so nothing on the frontend has to change
about how a row is keyed.
"""

from collections import defaultdict

from django.apps import apps
from django.db.models import Count
from django.utils import timezone

from .models import ActivityLog, CustomReport, ReportOverride
from .services import user_label
from .stats import currency_symbol, entity_stats, finance_stats, real_employees
from .utils import money


def _avatar(user, request):
    source = user.avatar or getattr(getattr(user, "profile", None), "profile_photo", None)
    if not source:
        return None
    url = source.url
    return request.build_absolute_uri(url) if request else url


def build_catalog(request, rng):
    Project = apps.get_model("projects", "Project")
    Task = apps.get_model("tasks", "Task")
    Client = apps.get_model("dashboard", "Client")

    sym = currency_symbol()
    now_iso = timezone.now().isoformat()
    stats = entity_stats()
    fin = finance_stats()

    projects = list(Project.objects.filter(is_archived=False).select_related("client").prefetch_related("team", "modules"))
    tasks = list(Task.objects.only("assignees", "status"))

    tasks_by_name = defaultdict(list)
    for t in tasks:
        for name in t.assignees or []:
            tasks_by_name[str(name).strip().lower()].append(t)

    activity_counts = {
        r["user_id"]: r["n"]
        for r in rng.apply(ActivityLog.objects.exclude(user__isnull=True)).values("user_id").annotate(n=Count("id"))
    }

    rows = []

    # ---- one per real employee (same filter as EmployeesPage.jsx)
    for u in real_employees().select_related("profile"):
        assigned = [p for p in projects if u in p.team.all() or p.manager_id == u.id]
        emp_tasks = tasks_by_name.get((u.name or "").strip().lower(), [])
        done = sum(1 for t in emp_tasks if t.status == "Completed")
        n_actions = activity_counts.get(u.id, 0)
        rows.append({
            "id": f"emp-{u.id}",
            "isAuto": True,
            "name": f"{user_label(u)} — Employee Report",
            "category": "Employee",
            "desc": (
                f"{u.role.title()} · {u.department or 'Unassigned department'}. "
                f"Assigned to {len(assigned)} project{'' if len(assigned) == 1 else 's'}, "
                f"{done}/{len(emp_tasks)} tasks completed. "
                f"{n_actions} action{'' if n_actions == 1 else 's'} recorded in this period."
            ),
            "dateRaw": (u.date_joined or timezone.now()).isoformat(),
            "by": "System",
            "avatar": _avatar(u, request),
            "format": "PDF",
            "userId": u.id,
        })

    # ---- one per project
    for p in projects:
        modules = list(p.modules.all())
        progress = round(100 * sum(1 for m in modules if m.status == "Completed") / len(modules)) if modules else None
        rows.append({
            "id": f"proj-{p.id}",
            "isAuto": True,
            "name": f"{p.name} — Project Report",
            "category": "Project",
            "desc": (
                f"Status: {p.status}{f' · {progress}% complete' if progress is not None else ''}. "
                f"Budget {sym}{money(p.budget)}, spent {sym}{money(p.spent)}."
            ),
            "dateRaw": p.updated_at.isoformat(),
            "by": "System",
            "avatar": None,
            "format": "Excel",
            "projectId": p.id,
        })

    # ---- one per client
    projects_by_client = defaultdict(list)
    for p in projects:
        if p.client_id:
            projects_by_client[p.client_id].append(p)
    for c in Client.objects.all():
        cps = projects_by_client.get(c.id, [])
        budget = sum(p.budget for p in cps)
        rows.append({
            "id": f"client-{c.id}",
            "isAuto": True,
            "name": f"{c.name} — Client Report",
            "category": "Client",
            "desc": f"{len(cps)} project{'' if len(cps) == 1 else 's'}, {sym}{money(budget)} total budget.",
            "dateRaw": c.created_at.isoformat(),
            "by": "System",
            "avatar": c.avatar_url or None,
            "format": "PDF",
            "clientId": c.id,
        })

    # ---- aggregates
    def agg(key, name, category, desc, fmt):
        rows.append({"id": key, "isAuto": True, "name": name, "category": category, "desc": desc,
                     "dateRaw": now_iso, "by": "System", "avatar": None, "format": fmt})

    if stats["totalBudget"] > 0:
        agg("agg-budget-vs-actual", "Budget vs Actual Report", "Financial",
            f"Total budget {sym}{money(stats['totalBudget'])} vs actual spend {sym}{money(stats['totalSpent'])} "
            f"across {stats['totalProjects']} project{'' if stats['totalProjects'] == 1 else 's'}.", "PDF")

    if fin["salesCount"]:
        agg("agg-revenue-by-client", "Revenue by Client Report", "Sales",
            f"{fin['salesCount']} sale{'' if fin['salesCount'] == 1 else 's'} totalling {sym}{money(fin['salesTotal'])}, "
            f"{sym}{money(fin['salesPaid'])} paid.", "Excel")
    elif stats["totalBudget"] > 0:
        n = len({p.client_id for p in projects if p.client_id})
        agg("agg-revenue-by-client", "Revenue by Client Report", "Sales",
            f"Budget allocation across {n} client{'' if n == 1 else 's'}.", "Excel")

    if fin["incomeCount"] or fin["expensesCount"]:
        agg("agg-income-vs-expenses", "Income vs Expenses Report", "Financial",
            f"{sym}{money(fin['incomeReceived'])} income received vs {sym}{money(fin['expensesApproved'])} approved expenses.", "PDF")

    if stats["totalTasks"] > 0:
        agg("agg-task-completion", "Task Completion Report", "Task",
            f"{stats['completedTasks']} of {stats['totalTasks']} tasks completed across all projects.", "PDF")
        if stats["overdueTasks"] > 0:
            n = stats["overdueTasks"]
            agg("agg-overdue-tasks", "Overdue Tasks Report", "Task",
                f"{n} task{'' if n == 1 else 's'} currently past due date.", "Excel")

    total_actions = sum(activity_counts.values())
    if total_actions:
        agg("agg-user-activity", "User Activity Report", "Employee",
            f"{total_actions} action{'' if total_actions == 1 else 's'} by {len(activity_counts)} "
            f"user{'' if len(activity_counts) == 1 else 's'} in this period.", "Excel")

    # ---- persisted rename / delete of auto rows
    overrides = {o.key: o for o in ReportOverride.objects.all()}
    visible = []
    for row in rows:
        o = overrides.get(row["id"])
        if o and o.hidden:
            continue
        if o and o.custom_name:
            row["name"] = o.custom_name
            row["manualEdit"] = True
        visible.append(row)

    # ---- rows an admin added by hand (Duplicate / Generate Custom Report)
    for c in CustomReport.objects.select_related("created_by"):
        visible.append({
            "id": f"custom-{c.id}",
            "isAuto": False,
            "name": c.name,
            "category": c.category,
            "desc": c.description,
            "dateRaw": c.created_at.isoformat(),
            "by": user_label(c.created_by) or "Unknown",
            "avatar": None,
            "format": c.format,
        })
    return visible
