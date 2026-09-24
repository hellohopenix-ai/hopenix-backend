# Same style as expenses/permissions.py and projects/permissions.py —
# plain functions the views call directly, reusing the existing
# `request.user.role` field instead of introducing a new permissions
# concept.


def can_approve_visitors(user):
    """Only admin can decide (Approve/Reject), ask a visitor to wait, or
    re-open an already-reviewed entry — mirrors VisitorsPage.jsx's own
    `isAdmin` gate on the Host Approval panel and its decide/askToWait/
    reopenForReview functions, enforced here too so it can never be
    reached by calling the API directly."""
    return getattr(user, "role", None) == "admin"


def can_edit_visitor(user, visitor):
    """Admin can always edit. Whoever registered the visitor can still
    fix a typo while it's still Waiting and unreviewed; once an admin has
    acted on it, only an admin can change it further."""
    if can_approve_visitors(user):
        return True
    return visitor.created_by_id == user.id and visitor.status == "Waiting" and not visitor.reviewed


def can_delete_visitor(user, visitor):
    return can_approve_visitors(user) or visitor.created_by_id == user.id
