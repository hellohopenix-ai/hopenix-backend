from rest_framework import permissions

from users.access import role_category


def is_admin(user):
    return bool(user and user.is_authenticated and user.role == "admin")


class IsStaff(permissions.BasePermission):
    """The Employees directory (list/detail) is intentionally viewable by
    every logged-in staff member -- EmployeesPage.jsx shows the whole table
    org-wide and only hides the Salary column per row (see
    EmployeeSerializer.get_salary for the matching server-side mask). The
    Client Portal has no equivalent page and must never reach this data, so
    this only refuses the "client" role category; it is not the fuller
    per-page ModuleAccess check other modules use."""

    message = "Employee records are not available for client accounts."

    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated) and role_category(getattr(user, "role", None)) != "client"


class IsAdmin(permissions.BasePermission):
    """Same convention as users.views.IsAdmin / projects & expenses
    permissions.py — plain role check, admin-only for anything that
    mutates shared employee data (status, rating, holidays, deciding a
    leave request, publishing/removing an announcement)."""

    def has_permission(self, request, view):
        return is_admin(request.user)


def can_manage_leave_request(user, leave_request):
    """Admin can always act. The employee who filed a still-pending
    request can cancel their own — but can't approve/reject it
    themselves (that always requires admin, enforced separately in the
    approve/reject view)."""
    if is_admin(user):
        return True
    return leave_request.employee_id == user.id and leave_request.status == "pending"