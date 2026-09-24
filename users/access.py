"""Server-side enforcement of the app's Page Access / Module Access rules.

Until now these rules (UserPage.jsx -> "Page Access Control", "Module Access
Control", "Individual User Access") were only applied by the React app --
they were stored in users.RolePermission / ModulePermission /
UserAccessOverride, but no API view ever read them, so anyone holding a
token could call the API directly and skip the UI.

This file re-implements, in Python, exactly what AuthContext.jsx already
does in the browser (getRoleCategory, getAllowedPages, getModulePermissions,
canAccessPage, canPerform), including the same built-in defaults, so the
server and the UI now agree:

    * admin (and Django superusers) -> everything
    * an individual override (users.UserAccessOverride) wins next:
        full -> everything, none -> nothing, custom -> only its page list
    * otherwise the role's saved row (RolePermission / ModulePermission)
    * otherwise the same hard-coded defaults AuthContext.jsx ships with

Use it from a view like this:

    from users.access import ModuleAccess

    class SomethingViewSet(viewsets.ModelViewSet):
        module_name = "Sales"                      # a page name from ALL_PAGES
        permission_classes = [permissions.IsAuthenticated, ModuleAccess]

GET/HEAD/OPTIONS need the module's "view" flag, POST "create", PUT/PATCH
"edit", DELETE "delete". Nothing else in the project imports this file yet,
so adding it changes no existing behaviour by itself.
"""

from rest_framework import permissions

# Must stay in sync with ALL_PAGES / DEFAULT_ROLE_PERMISSIONS /
# DEFAULT_MODULE_PERMISSIONS in frontend/src/AuthContext.jsx.
ALL_PAGES = [
    "Dashboard",
    "Projects",
    "Zip Files",
    "Tasks",
    "Meetings",
    "Visitors",
    "Clients",
    "Client Portal",
    "Employees",
    "Users",
    "Income",
    "Expenses",
    "Sales",
    "Reports",
    "Coworking Space",
    "Messages",
    "Settings",
]

DEFAULT_ROLE_PAGES = {
    "manager": ["Dashboard", "Projects", "Tasks", "Clients", "Client Portal", "Employees", "Income", "Expenses", "Sales", "Reports", "Coworking Space", "Visitors", "Messages", "Settings"],
    "employee": ["Dashboard", "Tasks", "Projects", "Messages", "Settings"],
    "client": ["Dashboard", "Projects", "Messages", "Settings"],
    "accountant": ["Dashboard", "Income", "Expenses", "Sales", "Reports", "Coworking Space", "Messages", "Settings"],
}

_FULL = {"view": True, "create": True, "edit": True, "delete": True}
_VIEW_ONLY = {"view": True, "create": False, "edit": False, "delete": False}
_NONE = {"view": False, "create": False, "edit": False, "delete": False}


def _defaults(overrides):
    base = {page: dict(_VIEW_ONLY) for page in ALL_PAGES}
    base.update(overrides)
    return base


DEFAULT_MODULE_FLAGS = {
    "manager": _defaults({
        "Dashboard": dict(_FULL),
        "Projects": dict(_FULL),
        "Tasks": dict(_FULL),
        "Clients": dict(_FULL),
        "Employees": {"view": True, "create": True, "edit": True, "delete": False},
        "Income": dict(_FULL),
        "Expenses": dict(_FULL),
        "Sales": dict(_FULL),
        "Reports": {"view": True, "create": True, "edit": False, "delete": False},
        "Messages": {"view": True, "create": True, "edit": True, "delete": False},
        "Settings": {"view": True, "create": False, "edit": True, "delete": False},
    }),
    "employee": _defaults({
        "Tasks": {"view": True, "create": True, "edit": True, "delete": False},
        "Messages": {"view": True, "create": True, "edit": False, "delete": False},
        "Employees": dict(_VIEW_ONLY),
        "Settings": {"view": True, "create": False, "edit": True, "delete": False},
    }),
    "client": _defaults({
        "Projects": dict(_VIEW_ONLY),
        "Messages": {"view": True, "create": True, "edit": False, "delete": False},
        "Settings": {"view": True, "create": False, "edit": True, "delete": False},
    }),
    "accountant": _defaults({
        "Income": dict(_FULL),
        "Expenses": dict(_FULL),
        "Sales": {"view": True, "create": True, "edit": True, "delete": False},
        "Reports": {"view": True, "create": True, "edit": False, "delete": False},
        "Messages": {"view": True, "create": True, "edit": False, "delete": False},
        "Settings": {"view": True, "create": False, "edit": True, "delete": False},
    }),
}

# Same map as ROLE_CATEGORY_MAP in AuthContext.jsx. Anything not listed is
# treated as "employee" (the most limited real-work bucket).
ROLE_CATEGORY_MAP = {
    "admin": "admin",
    "super admin": "admin",
    "manager": "manager",
    "project manager": "manager",
    "accountant": "accountant",
    "client": "client",
    "employee": "employee",
}

_ACTION_BY_METHOD = {
    "GET": "view",
    "HEAD": "view",
    "OPTIONS": "view",
    "POST": "create",
    "PUT": "edit",
    "PATCH": "edit",
    "DELETE": "delete",
}


def role_category(role):
    if not role:
        return "employee"
    return ROLE_CATEGORY_MAP.get(str(role).lower().strip(), "employee")


def _is_authenticated(user):
    return bool(user and getattr(user, "is_authenticated", False))


def _is_admin(user):
    return role_category(getattr(user, "role", None)) == "admin" or bool(getattr(user, "is_superuser", False))


def _cache(user):
    # One request = one user object, so keep the few lookups below on it
    # instead of hitting the database again for every check in that request.
    cache = getattr(user, "_hx_access_cache", None)
    if cache is None:
        cache = {}
        try:
            user._hx_access_cache = cache
        except Exception:
            pass
    return cache


def _override(user):
    from .models import UserAccessOverride

    cache = _cache(user)
    if "override" not in cache:
        cache["override"] = UserAccessOverride.objects.filter(user_id=user.pk).first()
    return cache["override"]


def allowed_pages(user):
    """Same result as getAllowedPages(role, userId) in AuthContext.jsx."""
    if not _is_authenticated(user):
        return []
    if _is_admin(user):
        return list(ALL_PAGES)

    override = _override(user)
    if override is not None:
        if override.mode == "full":
            return list(ALL_PAGES)
        if override.mode == "none":
            return []
        if override.mode == "custom":
            return list(override.pages or [])

    category = role_category(user.role)
    cache = _cache(user)
    if "role_pages" not in cache:
        from .models import RolePermission

        row = RolePermission.objects.filter(role=category).first()
        if row is not None:
            cache["role_pages"] = list(row.pages or [])
        else:
            cache["role_pages"] = list(DEFAULT_ROLE_PAGES.get(category, DEFAULT_ROLE_PAGES["employee"]))
    return cache["role_pages"]


def can_access_page(user, page):
    return page in allowed_pages(user)


def module_flags(user, module):
    """Same result as getModulePermissions(role, module, userId)."""
    if not _is_authenticated(user):
        return dict(_NONE)
    if _is_admin(user):
        return dict(_FULL)

    override = _override(user)
    if override is not None:
        if override.mode == "full":
            return dict(_FULL)
        if override.mode == "none":
            return dict(_NONE)
        if override.mode == "custom" and module not in (override.pages or []):
            return dict(_NONE)

    category = role_category(user.role)
    cache = _cache(user)
    key = ("module", category, module)
    if key not in cache:
        from .models import ModulePermission

        row = ModulePermission.objects.filter(role=category, module=module).first()
        if row is not None:
            cache[key] = {"view": row.view, "create": row.create, "edit": row.edit, "delete": row.delete}
        else:
            table = DEFAULT_MODULE_FLAGS.get(category, DEFAULT_MODULE_FLAGS["employee"])
            cache[key] = dict(table.get(module, _VIEW_ONLY))
    return cache[key]


def can_perform(user, action, module):
    """Same rule as canPerform(action, module) in AuthContext.jsx: a page the
    user can't open is "no access" for every action, whatever the module
    table says."""
    if not can_access_page(user, module):
        return False
    return bool(module_flags(user, module).get(action))


class ModuleAccess(permissions.BasePermission):
    """DRF permission: the view sets `module_name` (e.g. "Sales").

    Optional view attribute `block_clients` (default True): users whose role
    is "client" are refused outright, even if an admin's override would
    otherwise let them through -- clients use the Client Portal, never the
    staff modules.
    """

    message = "You do not have permission to access this module."

    def has_permission(self, request, view):
        user = request.user
        if not _is_authenticated(user):
            return False

        if getattr(view, "block_clients", True) and role_category(getattr(user, "role", None)) == "client":
            return False

        module = getattr(view, "module_name", None)
        if not module:
            # A view that forgot to declare its module must fail closed.
            return False

        action = _ACTION_BY_METHOD.get(request.method)
        if action is None:
            return False
        return can_perform(user, action, module)