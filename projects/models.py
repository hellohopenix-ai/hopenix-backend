import uuid
from django.conf import settings
from django.db import models


class StatusChoices(models.TextChoices):
    IN_PROGRESS = "In Progress", "In Progress"
    IN_REVIEW = "In Review", "In Review"
    COMPLETED = "Completed", "Completed"
    ON_HOLD = "On Hold", "On Hold"
    CANCELLED = "Cancelled", "Cancelled"


class PriorityChoices(models.TextChoices):
    LOW = "Low", "Low"
    MEDIUM = "Medium", "Medium"
    HIGH = "High", "High"


class ModuleStatusChoices(models.TextChoices):
    PENDING = "Pending", "Pending"
    IN_PROGRESS = "In Progress", "In Progress"
    COMPLETED = "Completed", "Completed"


# Har file disk par ek predictable, per-project folder ke andar jaati hai
# (MEDIA_ROOT/...). FileField sirf ye relative path Postgres mein save
# karta hai — asal bytes kabhi database mein nahi jaate (base64 wala
# purana localStorage tareeqa yahan use nahi ho raha).
def project_brief_path(instance, filename):
    return f"projects/{instance.id}/brief/{uuid.uuid4()}_{filename}"


def project_zip_path(instance, filename):
    return f"projects/{instance.id}/deliverable/{uuid.uuid4()}_{filename}"


def module_file_path(instance, filename):
    return f"projects/{instance.module.project_id}/modules/{instance.module_id}/{uuid.uuid4()}_{filename}"


class Project(models.Model):
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    project_type = models.CharField(
        max_length=20, choices=[("client", "Client"), ("company", "Company")], default="company"
    )

    # Existing Client model dashboard app mein hai — usi ko reference
    # karte hain, alag se client list dobara nahi banate.
    client = models.ForeignKey(
        "dashboard.Client", null=True, blank=True, on_delete=models.SET_NULL, related_name="projects"
    )

    manager = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="managed_projects"
    )
    team = models.ManyToManyField(settings.AUTH_USER_MODEL, blank=True, related_name="project_memberships")

    status = models.CharField(max_length=20, choices=StatusChoices.choices, default=StatusChoices.IN_PROGRESS)
    priority = models.CharField(max_length=10, choices=PriorityChoices.choices, default=PriorityChoices.MEDIUM)

    budget = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    spent = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    # FIX (ProjectsPage.jsx: features/requirements/additionalInfo/
    # completionLink never reached the backend): these 4 fields were read
    # and written by the frontend form on every create/edit, but had no
    # matching column here — so the request payload silently dropped them
    # and they only ever lived in local component state / localStorage,
    # vanishing for anyone opening the project from a different session.
    features = models.TextField(blank=True, default="")
    requirements = models.TextField(blank=True, default="")
    additional_info = models.TextField(blank=True, default="")
    completion_link = models.URLField(max_length=500, blank=True, default="")

    start_date = models.DateField(null=True, blank=True)
    due_date = models.DateField(null=True, blank=True)

    brief = models.FileField(upload_to=project_brief_path, null=True, blank=True)
    brief_uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    completed_zip = models.FileField(upload_to=project_zip_path, null=True, blank=True)
    zip_uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    # Zip Files page (ZipFilesPage.jsx) ko exactly ye teen cheezein chahiye
    # the har row ke liye: asal file ka naam, uska size, aur kab upload
    # hui — teeno ab yahan Postgres mein explicitly save hote hain (upload
    # ke waqt hi), taake FileField.size jaisi storage-hit calls baar baar
    # na karni parein jab poori zip list load ho (N projects = N disk
    # reads warna). Ye purani base64 dataUrl wali approach ka replacement
    # hai — ab sirf ye chhota sa metadata + relative path DB mein hai,
    # asal bytes hamesha disk (MEDIA_ROOT) par hain.
    zip_original_name = models.CharField(max_length=255, blank=True, default="")
    zip_size = models.PositiveBigIntegerField(default=0)  # bytes
    zip_uploaded_at = models.DateTimeField(null=True, blank=True)

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="created_projects")
    is_archived = models.BooleanField(default=False)  # soft-delete — ek click se data hamesha ke liye khatam nahi hota

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        indexes = [
            models.Index(fields=["manager"]),
            models.Index(fields=["is_archived"]),
        ]

    def __str__(self):
        return self.name

    @property
    def top_level_modules(self):
        # Everything shown as its own row in a project's checklist —
        # sub-tasks (Module.parent set) nest under their parent's own
        # `subtasks` instead of appearing again here as a duplicate flat
        # entry. See ModuleSerializer / ClientProjectModuleSerializer.
        return self.modules.filter(parent__isnull=True)


class Module(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="modules")
    name = models.CharField(max_length=255)
    assignee = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="assigned_modules"
    )
    status = models.CharField(max_length=20, choices=ModuleStatusChoices.choices, default=ModuleStatusChoices.PENDING)
    priority = models.CharField(max_length=10, choices=PriorityChoices.choices, default=PriorityChoices.MEDIUM)
    due_date = models.DateField(null=True, blank=True)
    url = models.URLField(blank=True, default="")
    # NEW — same review gate ModuleFile.approved gives uploaded files, but for
    # the live/staging link. Before this, a link pasted on the Task Page went
    # straight onto the Client Portal with no admin review at all (only files
    # were gated). Any change to `url` resets this to False (see
    # projects/views.py ModuleViewSet.perform_update) so an already-approved
    # link can't be swapped for a different one behind the admin's back.
    url_approved = models.BooleanField(default=False)
    url_approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    url_approved_at = models.DateTimeField(null=True, blank=True)
    price = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    # NEW — real backend row for ClientsPage.jsx's "sub-modules" (e.g. a
    # "Development" module broken into Frontend/Backend/Database/API
    # Integration sub-tasks, each with its own tick + attachments). This
    # used to be a purely local-only refinement on top of a single flat
    # Module row — a sub-task is just another Module, self-referencing
    # its parent, so it gets the exact same status/unlock/ModuleFile
    # machinery every top-level module already has for free.
    # null = a normal top-level module; set = this IS a sub-task.
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.CASCADE, related_name="subtasks"
    )

    # NEW — mirrors ClientsPage.jsx's module.unlocked: only the first
    # module of a new project starts True; every module after it stays
    # False until the client requests it (dashboard.ModuleRequest) and
    # staff accepts that request. Defaults to True so existing modules
    # created before this field existed don't suddenly all show as locked.
    unlocked = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.project.name} / {self.name}"


class ModuleFile(models.Model):
    module = models.ForeignKey(Module, on_delete=models.CASCADE, related_name="files")
    file = models.FileField(upload_to=module_file_path)
    original_name = models.CharField(max_length=255)
    mime_type = models.CharField(max_length=100, blank=True, default="")
    size = models.PositiveBigIntegerField(default=0)

    # NEW — the review/approval gate between "assignee uploaded it" and
    # "client can see it on the Portal". Every upload lands as False
    # (pending_review, in the Client Page's own wording); staff ticks
    # "Approved / Show on Portal" to flip it True. dashboard/serializers.py
    # _module_attachments() hides not-yet-approved files from anyone
    # viewing as a client, so a Task Page upload never becomes visible on
    # the Client Portal until an admin/manager has actually reviewed it —
    # independent of the module's own lock/unlock (payment) state.
    approved = models.BooleanField(default=False)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    approved_at = models.DateTimeField(null=True, blank=True)

    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="uploaded_files")
    uploaded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.original_name