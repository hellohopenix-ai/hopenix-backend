"""Single source of truth for which per-project commissions really count
towards a person's earnings.

A commission row counts ONLY while:
  * its task is Completed, or (for a project-level row) its project is
    Completed and not archived; and
  * the task / project still exists (a deleted one leaves the row with
    task=NULL and project=NULL -> "removed", never counted); and
  * the project is not archived (= deactivated).

Everything that shows a total (Users page, Employees page) must use
`counted_total_for()` so the number can never differ between pages.
"""

from decimal import Decimal

from django.db.models import Q, Sum

COMPLETED = "Completed"


def counted_q():
    return (
        Q(task__isnull=False, task__status=COMPLETED)
        | Q(
            task__isnull=True,
            project__isnull=False,
            project__status=COMPLETED,
            project__is_archived=False,
        )
    )


def counted_total_for(user):
    total = user.commissions.filter(counted_q()).aggregate(t=Sum("amount"))["t"]
    return float(total or Decimal("0"))


def commission_state(row):
    """-> (state, label). state is one of: counted | pending | removed."""
    if row.task_id is None and row.project_id is None:
        return "removed", "Project/Task removed or deactivated"
    if row.task_id is None and row.project is not None and row.project.is_archived:
        return "removed", "Project removed or deactivated"
    linked = row.task if row.task_id else row.project
    if linked is not None and linked.status == COMPLETED:
        return "counted", COMPLETED
    return "pending", (linked.status if linked is not None else "")
