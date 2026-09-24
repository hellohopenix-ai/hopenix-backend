from rest_framework import serializers

from dashboard.models import Client
from projects.models import Module
from .models import Task, TaskZipFile
from .attachments import merge_attachments


class TaskZipFileSerializer(serializers.ModelSerializer):
    """Ek row = task par attach ki gayi ek .zip file. Same shape jo
    projects/serializers.py ka ProjectZipSerializer deta hai, taake Zip
    Files (admin) page dono (project deliverables + task zip
    attachments) ko ek hi merged list mein, bina alag-alag if/else ke,
    render kar sake — sirf `source` field se pata chalta hai ye kahan
    se aayi."""

    id = serializers.SerializerMethodField()
    projectName = serializers.SerializerMethodField()
    client = serializers.CharField(source="task.client.name", read_only=True, default="")
    projectDetails = serializers.CharField(source="task.description", read_only=True, default="")
    fileName = serializers.CharField(source="original_name", read_only=True)
    uploadedBy = serializers.CharField(source="uploaded_by.name", read_only=True, default="")
    uploadedOn = serializers.DateTimeField(source="uploaded_at", read_only=True)
    size = serializers.SerializerMethodField()
    downloadUrl = serializers.SerializerMethodField()
    deleteUrl = serializers.SerializerMethodField()
    source = serializers.SerializerMethodField()

    class Meta:
        model = TaskZipFile
        fields = [
            "id", "projectName", "client", "projectDetails", "fileName",
            "uploadedBy", "uploadedOn", "size", "downloadUrl", "deleteUrl", "source",
        ]

    def get_id(self, obj):
        return f"task-{obj.id}"

    def get_projectName(self, obj):
        # Task ka apna "project" field sirf ek plain naam string hai
        # (FK nahi) — isliye "<project> — <task title>" dikhate hain
        # taake list mein pata chale ye zip kis task ki hai.
        return f"{obj.task.project} — {obj.task.title}" if obj.task.project else obj.task.title

    def get_size(self, obj):
        return round((obj.size or 0) / (1024 * 1024), 2)

    def get_downloadUrl(self, obj):
        request = self.context.get("request")
        path = f"/api/tasks/tasks/{obj.task_id}/zip/{obj.id}/download/"
        return request.build_absolute_uri(path) if request else path

    def get_deleteUrl(self, obj):
        request = self.context.get("request")
        path = f"/api/tasks/tasks/{obj.task_id}/zip/{obj.id}/"
        return request.build_absolute_uri(path) if request else path

    def get_source(self, obj):
        return "task"


class TaskSerializer(serializers.ModelSerializer):
    """Field names here are deliberately camelCase (dueDate, createdBy,
    clientId, sampleFiles, ...) so this is a drop-in JSON shape for
    TasksPage.jsx — every place that page does `task.dueDate`,
    `task.clientId`, `task.sampleFiles`, etc. reads straight off what
    this serializer returns, same as UserSerializer does for
    AuthContext/UserPage."""

    clientId = serializers.PrimaryKeyRelatedField(
        source="client", queryset=Client.objects.all(), allow_null=True, required=False
    )
    clientName = serializers.CharField(source="client.name", read_only=True)

    assigneeRole = serializers.CharField(source="assignee_role", required=False, allow_blank=True)
    dueDate = serializers.DateField(source="due_date", required=False, allow_null=True)
    createdBy = serializers.CharField(source="created_by", required=False, allow_blank=True)
    createdOn = serializers.DateField(source="created_on", read_only=True)

    sampleFiles = serializers.JSONField(source="sample_files", required=False)

    moduleName = serializers.CharField(source="module_name", required=False, allow_blank=True)
    moduleProjectName = serializers.CharField(source="module_project_name", required=False, allow_blank=True)
    moduleTaskKey = serializers.CharField(source="module_task_key", required=False, allow_blank=True)
    roleTemplate = serializers.CharField(source="role_template", required=False, allow_blank=True)
    requiresLink = serializers.BooleanField(source="requires_link", required=False)
    fromClientModule = serializers.BooleanField(source="from_client_module", required=False)

    # FIX: expose the real backend Module pk as moduleBackendId so the
    # frontend can stamp it on auto-created tasks and later drive
    # cross-browser ModuleFile uploads without needing localStorage.
    moduleBackendId = serializers.PrimaryKeyRelatedField(
        source="module",
        queryset=Module.objects.all(),
        allow_null=True,
        required=False,
    )
    # Derived read-only: the Module's own project pk — avoids a second
    # lookup on the frontend when building the uploadModuleFile URL.
    moduleProjectBackendId = serializers.SerializerMethodField()

    def get_moduleProjectBackendId(self, obj):
        return obj.module.project_id if obj.module_id else None

    class Meta:
        model = Task
        fields = [
            "id", "title", "description", "requirements",
            "project", "clientId", "clientName",
            "assignees", "assigneeRole",
            "priority", "status", "dueDate",
            "createdBy", "createdOn",
            "progress", "locked",
            "attachment", "attachments", "sampleFiles", "subtasks",
            "moduleName", "moduleProjectName", "moduleTaskKey",
            "roleTemplate", "requiresLink", "fromClientModule",
            "moduleBackendId", "moduleProjectBackendId",
        ]

    def to_representation(self, instance):
        data = super().to_representation(instance)
        # Old rows already hold the same file twice (see attachments.py) —
        # collapse them on the way out so every viewer sees each file once.
        if isinstance(data.get("attachments"), list):
            data["attachments"] = merge_attachments(data["attachments"])
        return data

    def validate_assignees(self, value):
        if not isinstance(value, list) or not (1 <= len(value) <= 3):
            raise serializers.ValidationError("A task needs 1 to 3 assignees.")
        return value


class TaskBulkCreateItemSerializer(TaskSerializer):
    """Same shape as TaskSerializer, just without the assignee-count
    validation duplicated per-module — used by TaskViewSet.bulk_create
    for role-template tasks (one row per module, all sharing the same
    assignees/sampleFiles — mirrors the `createdList` map in
    TasksPage.jsx's CreateTaskModal onCreate handler)."""

    pass


class TaskCompleteSerializer(serializers.Serializer):
    """POST body for /api/tasks/tasks/{id}/complete/ — mirrors the
    (link, newAttachments) arguments markCompleted() takes on the
    frontend."""

    link = serializers.CharField(required=False, allow_blank=True)
    attachments = serializers.ListField(child=serializers.JSONField(), required=False)


class TaskAddAttachmentSerializer(serializers.Serializer):
    """POST body for /api/tasks/tasks/{id}/add-attachment/ — mirrors
    addTaskAttachment(id, attachment) on the frontend: lets a
    screenshot/video/link be added any time, not just at completion."""

    attachment = serializers.JSONField()


class TaskToggleSubtaskSerializer(serializers.Serializer):
    """POST body for /api/tasks/tasks/{id}/toggle-subtask/ — mirrors
    toggleSubtask(taskId, subId) on the frontend."""

    subtaskId = serializers.IntegerField()