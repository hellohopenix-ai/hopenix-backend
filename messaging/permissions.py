from django.contrib.auth import get_user_model
from django.db.models import Q

User = get_user_model()


def _project_teammate_ids(user):
    """FIX (Projects page: file/ZIP attach never reached Messages): a
    project's manager/team is set on projects.Project (ProjectsPage.jsx),
    completely separate from the User.manager "who reports to whom"
    relationship get_allowed_contacts() below is built on. So a manager
    sending a project brief/ZIP/module-file to their own project's team
    (via messagesApi.sendMessage -> SendMessageView -> can_message) was
    silently 403ing for anyone who wasn't ALSO their literal direct
    report — the frontend swallows that error (`.catch(() => {})`), so
    it just looked like "attaching a file doesn't send anything".
    Pulls in everyone `user` shares an active (non-archived) project
    with, as manager, team member, or creator — on EITHER side — so two
    people on the same project can always message each other,
    regardless of the org-chart relationship above.
    Deferred import: projects app never imports messaging, so this is a
    safe one-directional dependency, but importing at module load time
    would still trip Django's app-loading order — hence imported here,
    inside the function, same as views.py already does for the `tasks`
    app in projects/views.py's zip_files().
    """
    from projects.models import Project

    ids = set()
    projects = Project.objects.filter(is_archived=False).filter(
        Q(manager=user) | Q(team=user) | Q(created_by=user)
    )
    for p in projects.select_related("manager", "created_by").prefetch_related("team"):
        if p.manager_id:
            ids.add(p.manager_id)
        if p.created_by_id:
            ids.add(p.created_by_id)
        ids.update(p.team.values_list("id", flat=True))
    ids.discard(user.id)
    return ids


def _client_manager_ids(user):
    """FIX (add Client -> auto-assigned task's Message never arrives):
    TasksPage.jsx auto-assigns a task to a Client's manager the moment
    that client is created/assigned (see dashboard.Client.manager) and
    immediately tries to send them a real chat message about it — but
    that assignment doesn't create/populate a matching projects.Project
    row (and its `team`) at the same time, so _project_teammate_ids()
    above has nothing to go on yet and can_message() was silently
    403ing the very first "you've been assigned" notification.
    Mirrors _project_teammate_ids' shape: pulls in, on either side,
    whoever manages a Client `user` created/administers, and whoever
    created/administers a Client `user` manages — so an admin/whoever
    added the client and that client's assigned manager can always
    reach each other, independent of whether a Project row exists yet.
    Deferred import for the same app-loading-order reason documented
    on _project_teammate_ids above (dashboard never imports messaging).
    """
    from dashboard.models import Client

    ids = set()
    clients = Client.objects.filter(Q(manager=user) | Q(created_by=user))
    for c in clients.select_related("manager", "created_by"):
        if c.manager_id:
            ids.add(c.manager_id)
        if c.created_by_id:
            ids.add(c.created_by_id)
    ids.discard(user.id)
    return ids


def _task_coworker_ids(user):
    """BUG 4 FIX (POST /api/messages/send/ → 403 on task completion):
    When an employee completes a task and notifyAdminOfTaskCompletion()
    resolves task.createdBy to a manager who is NOT their direct
    User.manager (org-chart) AND they share no projects.Project,
    can_message() fails in BOTH directions — the task-creator relationship
    isn't covered by the org-chart or project-teammate rules above.

    This mirrors _project_teammate_ids and _client_manager_ids: pulls in
    every user who shares a Task with `user` either as creator or assignee.
    Task.assignees is a JSON list of display-name strings, so we match
    against User.name / User.username exactly as notifyAdminOfTaskCompletion
    does on the frontend.

    Deferred import: tasks app never imports messaging (would be circular),
    so this safe one-directional import lives inside the function, same as
    the project and dashboard imports above.
    """
    from tasks.models import Task

    user_names = set()
    if getattr(user, "name", ""):
        user_names.add(user.name.strip().lower())
    if getattr(user, "username", ""):
        user_names.add(user.username.strip().lower())

    # Tasks where user is creator — find all assignee names from those tasks.
    created_tasks = Task.objects.filter(created_by__in=user_names).values_list("assignees", flat=True)
    # Tasks where user is an assignee — find the creator names from those tasks.
    # assignees__contains=[name] does a Postgres JSON containment check.
    assigned_tasks_creators = set()
    for name in user_names:
        assigned_tasks_creators.update(
            Task.objects.filter(assignees__contains=[name]).values_list("created_by", flat=True)
        )

    # Collect every display-name that appeared in a task alongside this user.
    coworker_names: set[str] = set()
    for assignee_list in created_tasks:
        for n in (assignee_list or []):
            if n:
                coworker_names.add(n.strip().lower())
    for n in assigned_tasks_creators:
        if n:
            coworker_names.add(n.strip().lower())
    coworker_names -= user_names  # exclude self

    if not coworker_names:
        return set()

    # Resolve display names → real approved User ids (same lookup the
    # frontend uses in idByName: case-insensitive on User.name).
    from django.db.models import functions
    ids = set(
        User.objects.filter(status="approved")
        .exclude(role="client")
        .annotate(name_lower=functions.Lower("name"))
        .filter(name_lower__in=coworker_names)
        .values_list("id", flat=True)
    )
    ids.discard(user.id)
    return ids



def get_allowed_contacts(user):
    """Who `user` is allowed to see/message on the Messages page.

    - admin: every OTHER approved STAFF user (can message anyone on the
      team — but never a client; a client's chat lives on the separate
      Client Portal messaging system, dashboard.ClientMessage, not here).
    - manager: Admin(s) + their own manager (if any) + their own team
      members (the people who report to them) — all approved.
    - everyone else (employee/client/accountant/...): Admin(s) + their
      own assigned manager only — this is the rule the models.py
      comment on `User.manager` already documents.
    - EVERYONE also gets anyone they share an active project with (see
      _project_teammate_ids above), and anyone linked to them through a
      Client they manage/created (see _client_manager_ids above) — both
      are their own separate relationship from the org-chart one above,
      and those people need to be able to message each other regardless
      of who reports to whom.

    Always excludes the user themself, non-approved accounts, and any
    role="client" account (portal logins show up on the Client Portal's
    own messaging, never on the staff Messages page).
    Returns a queryset (not a list of ids) so callers can order it, use
    it directly in ContactSerializer, etc.
    """
    base = User.objects.filter(status="approved").exclude(id=user.id).exclude(role="client")

    if user.role == "admin":
        return base

    project_ids = _project_teammate_ids(user)
    client_ids = _client_manager_ids(user)
    task_ids = _task_coworker_ids(user)

    if user.role == "manager":
        allowed_ids = set(base.filter(role="admin").values_list("id", flat=True))
        if user.manager_id:
            allowed_ids.add(user.manager_id)
        allowed_ids.update(base.filter(manager_id=user.id).values_list("id", flat=True))
        allowed_ids.update(project_ids)
        allowed_ids.update(client_ids)
        allowed_ids.update(task_ids)
        return base.filter(id__in=allowed_ids)

    # Default (employee/client/accountant/etc.): admin(s) + assigned manager
    # + project teammates + client managers + task coworkers.
    allowed_ids = set(base.filter(Q(role="admin") | Q(id=user.manager_id)).values_list("id", flat=True))
    allowed_ids.update(project_ids)
    allowed_ids.update(client_ids)
    allowed_ids.update(task_ids)
    return base.filter(id__in=allowed_ids)


def get_allowed_contact_ids(user):
    """Same rule as get_allowed_contacts(), as a set of ids — handy for
    quick `.filter(id__in=...)` / membership checks elsewhere."""
    return set(get_allowed_contacts(user).values_list("id", flat=True))


def can_message(user, other_user):
    """Whether `user` is allowed to open/send a direct conversation with
    `other_user`. Symmetric in practice (if A can see B, B — being admin
    or B's own manager relationship — can generally see A back), but we
    check both directions explicitly so a message can never be created
    that one side isn't allowed to have started."""
    if user.id == other_user.id:
        return False
    return other_user.id in get_allowed_contact_ids(user) or user.id in get_allowed_contact_ids(other_user)