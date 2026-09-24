import uuid
from django.conf import settings
from django.db import models

from dashboard.models import Client


class Task(models.Model):
    """Backs TasksPage.jsx — every field here is named/shaped to match
    exactly what that page already reads/writes to `taskspage_tasks_v1`
    in localStorage, so swapping the page over to this API is a drop-in
    (same task object shape in, same shape out — see TaskSerializer).

    `assignees` stays a plain JSON list of display-name strings (not a
    FK to users), because that's what TasksPage.jsx itself stores and
    matches against everywhere (getAssignees(), resolveRealAssignee(),
    AssigneeStack, ...) — changing that to a relation would be a real
    behaviour change, not just "give it a backend"."""

    PRIORITY_CHOICES = [
        ("High", "High"),
        ("Medium", "Medium"),
        ("Low", "Low"),
    ]

    STATUS_CHOICES = [
        ("Pending", "Pending"),
        ("In Progress", "In Progress"),
        ("In Review", "In Review"),
        ("Completed", "Completed"),
        ("Overdue", "Overdue"),
    ]

    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    requirements = models.TextField(blank=True)

    project = models.CharField(max_length=255)
    # Optional link to a real Clients-page client (dashboard.Client) —
    # mirrors task.clientId / task.clientName in the frontend. Nullable:
    # plenty of tasks have no client at all.
    client = models.ForeignKey(
        Client, on_delete=models.SET_NULL, null=True, blank=True, related_name="tasks"
    )

    # Up to 3 people, stored as display names — matches getAssignees().
    assignees = models.JSONField(default=list, blank=True)
    assignee_role = models.CharField(max_length=150, blank=True)

    priority = models.CharField(max_length=10, choices=PRIORITY_CHOICES, default="Medium")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="Pending")
    due_date = models.DateField(null=True, blank=True)

    created_by = models.CharField(max_length=150, blank=True)
    created_on = models.DateField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    progress = models.PositiveSmallIntegerField(default=0)
    # Locked module task (client hasn't requested / staff hasn't accepted
    # it yet) — markCompleted() on the frontend refuses to complete these.
    locked = models.BooleanField(default=False)

    # Single "primary" link/URL, kept for backwards compatibility with
    # older tasks (task.attachment), plus the full list of everything
    # attached over the task's life (screenshots, videos, zips, links —
    # task.attachments). Same for sampleFiles (reference files attached
    # at creation) and subtasks (checklist items).
    attachment = models.CharField(max_length=1000, blank=True)
    attachments = models.JSONField(default=list, blank=True)
    sample_files = models.JSONField(default=list, blank=True)
    subtasks = models.JSONField(default=list, blank=True)

    # Role-template / client-module metadata — set when a task was
    # generated from a role template (Graphic Designer, Video Editor, ...)
    # or auto-created from a client's project module.
    module_name = models.CharField(max_length=255, blank=True)
    module_project_name = models.CharField(max_length=255, blank=True)
    module_task_key = models.CharField(max_length=255, blank=True)
    role_template = models.CharField(max_length=100, blank=True)
    requires_link = models.BooleanField(default=False)
    from_client_module = models.BooleanField(default=False)

    # FIX: direct FK to the real projects.Module this task belongs to.
    # Null for tasks that pre-date this fix or have no module. Used by
    # upload_zip / upload_file_attachment to create ModuleFile rows so
    # ClientsPage and ClientPortal can display the file without depending
    # on any browser's localStorage.
    module = models.ForeignKey(
        "projects.Module",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="tasks",
    )

    class Meta:
        ordering = ["-id"]

    def __str__(self):
        return self.title


# Har file disk par ek predictable per-task folder ke andar jaati hai
# (MEDIA_ROOT/tasks/<task_id>/zip/...) — projects app ke completed_zip
# jaisa hi tareeqa, farq sirf itna hai ke ek task par ek se zyada zip
# attach ho sakti hain (Project par sirf ek "completed_zip"), isliye
# alag row-per-file model chahiye tha, ek hi FileField nahi.
def task_zip_path(instance, filename):
    return f"tasks/{instance.task_id}/zip/{uuid.uuid4()}_{filename}"


class TaskZipFile(models.Model):
    """Task page se attach ki gayi .zip files — asal bytes disk par,
    Postgres mein sirf path + metadata (Zip Files (admin) page isi
    model ko Project.completed_zip ke saath merge karke ek list
    dikhata hai — dekho projects/views.py ka zip_files action)."""

    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="zip_files")
    file = models.FileField(upload_to=task_zip_path)
    original_name = models.CharField(max_length=255)
    size = models.PositiveBigIntegerField(default=0)  # bytes

    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-uploaded_at"]

    def __str__(self):
        return self.original_name