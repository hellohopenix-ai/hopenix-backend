from django.db.models import Q
from rest_framework import permissions


# users/views.py mein pehle se maujood IsAdmin (request.user.role == "admin")
# jaisa hi pattern — isi field ko dobara use karte hain, koi naya role
# concept introduce nahi kar rahe.

def visible_projects_queryset(user, base_queryset):
    """
    THE CORE VISIBILITY RULE:
    - Admin        -> har (non-archived) project, koi filter nahi.
    - Baaki sab    -> sirf wo project jahan wo manager hai, YA team mein
                      hai, YA usi ne banaya (created_by).

    Filter DB-query level par lagta hai (.filter(...)), isliye normal user
    ki request Postgres se kisi doosre ka project row le hi kar nahi aati.
    """
    qs = base_queryset.filter(is_archived=False)

    if user.role == "admin":
        return qs

    return qs.filter(Q(manager=user) | Q(team=user) | Q(created_by=user)).distinct()


def can_access_project(user, project):
    """Read access same rule, ek already-loaded object par."""
    if user.role == "admin":
        return True
    if project.created_by_id == user.id:
        return True
    if project.manager_id == user.id:
        return True
    return project.team.filter(id=user.id).exists()


def can_manage_project(user, project):
    """Write access (edit/module add/file upload): admin, manager, ya team member."""
    return can_access_project(user, project)


def can_approve_project_content(user, project):
    """Who may tick \"Approved / Show on Portal\" on a submitted file or link:
    admin, or the project's own manager. Deliberately NOT can_manage_project —
    that also lets every plain team member through, which would let the
    employee who uploaded something approve their own work onto the Client
    Portal and skip the review step entirely."""
    if user.role == "admin":
        return True
    return project.manager_id == user.id


def can_access_file(user, project, module_file):
    """Mirrors frontend's canSeeFile: admin YA file ka uploader YA project manager."""
    if user.role == "admin":
        return True
    if module_file.uploaded_by_id == user.id:
        return True
    return project.manager_id == user.id


class ProjectObjectPermission(permissions.BasePermission):
    def has_object_permission(self, request, view, obj):
        if request.method in permissions.SAFE_METHODS:
            return can_access_project(request.user, obj)
        return can_manage_project(request.user, obj)