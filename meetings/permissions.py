# Same style as expenses/permissions.py / projects/permissions.py — plain
# functions the views call directly, reusing request.user.role and the
# existing users.UserSubPageAccess model instead of introducing a new
# permissions concept.
#
# Mirrors the frontend's own check exactly (see Meetings.jsx):
#
#   const isAdminOrManager =
#     user?.role === "admin" ||
#     getRoleCategory(user?.role) === "manager" ||
#     hasFullSubPageAccess("Meetings");


def is_admin_or_manager(user):
    """True for admins, managers, and anyone individually granted "full"
    access to the Meetings page via UserSubPageAccess (UserPage.jsx's
    per-user "Manage Access" -> Meetings tab override)."""
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if user.role in ("admin", "manager"):
        return True
    return user.sub_page_access.filter(page="Meetings", mode="full").exists()


def can_manage_meetings(user):
    """Schedule/decline/cancel a meeting directly, reschedule a meeting
    directly, set its meeting link, review a meeting/reschedule request,
    edit shared availability — everything AdminMeetingsView can do that
    UserMeetings can't."""
    return is_admin_or_manager(user)
