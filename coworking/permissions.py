# Same style as expenses/permissions.py and visitors/permissions.py --
# plain functions the views call directly, reusing the existing
# `request.user.role` field instead of introducing a new permissions concept.
from rest_framework import permissions

# CoworkingSpacePage.jsx's own rule: "Admin: full access (form + approvals).
# Manager: everything except approvals." Nobody else (employee, client...)
# gets in -- these are people's ID-card scans and the rent ledger. Add a role
# here (e.g. "accountant") if you later give it the Coworking Space page.
STAFF_ROLES = ("admin", "manager")


def can_use_coworking(user):
    """List / view applications, submit one, tick a month's rent as received."""
    return bool(user and user.is_authenticated and getattr(user, "role", None) in STAFF_ROLES)


def can_review_coworking(user):
    """Approve / reject / hold an application (Section J), and delete
    records -- mirrors the page's own `isAdmin` gate on both."""
    return bool(user and user.is_authenticated and getattr(user, "role", None) == "admin")


class IsCoworkingStaff(permissions.BasePermission):
    message = "You don't have access to Coworking Space."

    def has_permission(self, request, view):
        return can_use_coworking(request.user)
