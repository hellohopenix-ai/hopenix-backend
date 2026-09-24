# Same style as meetings/permissions.py / expenses/permissions.py — plain
# functions the views call directly, reusing request.user.role and the
# existing users.UserSubPageAccess model instead of a new permissions concept.
#
# Mirrors the frontend exactly (ReportsPage.jsx):
#
#   const isAdmin = hasFullSubPageAccess("Reports");

from rest_framework import permissions

from users.access import role_category


def has_full_reports_access(user):
    """True for admins, and for anyone an admin individually granted "Full
    Reports Access" (UserPage.jsx -> Manage Access -> Reports tab, stored as
    UserSubPageAccess(page="Reports", mode="full"))."""
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if user.role == "admin" or getattr(user, "is_superuser", False):
        return True
    return user.sub_page_access.filter(page="Reports", mode="full").exists()


class ReportsStaffAccess(permissions.BasePermission):
    """Every /api/reports/ endpoint: logged-in staff only.

    Users whose role is "client" are refused outright -- clients use the
    Client Portal, never Reports (the React app already hides the page from
    them; this makes the server agree). Everything finer-grained stays where
    it was: non-admins only ever see / delete their own rows, and the
    admin-wide endpoints still call require_full() themselves. Same rule as
    employees/permissions.IsStaff, so employees keep submitting daily
    reports exactly as before.
    """

    message = "Clients cannot access Reports."

    def has_permission(self, request, view):
        user = request.user
        if not (user and getattr(user, "is_authenticated", False)):
            return False
        return role_category(getattr(user, "role", None)) != "client"