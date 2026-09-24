# Same style as projects/permissions.py — plain functions the views
# call directly, reusing the existing `request.user.role` field instead
# of introducing a new permissions concept.


def can_review_expenses(user):
    """Admin/accountant can approve or reject any expense."""
    return user.role in ("admin", "accountant")


def can_edit_expense(user, expense):
    """Admin/accountant can always edit. The person who logged the
    expense can still edit it too, but only while it's still Pending —
    once it's Approved/Rejected it's locked for them (soft-lock, same
    spirit as projects.Project.is_archived)."""
    if can_review_expenses(user):
        return True
    return expense.created_by_id == user.id and expense.status == "Pending"


def can_delete_expense(user, expense):
    return can_review_expenses(user) or expense.created_by_id == user.id
