from rest_framework import serializers
from .models import Project, Module, ModuleFile


class ProjectZipSerializer(serializers.ModelSerializer):
    """Ek row = ek project jiski completed_zip lagi hui hai. Same shape
    jo tasks/serializers.py ka TaskZipFileSerializer deta hai (id,
    downloadUrl, deleteUrl, source, ...) — Zip Files (admin) page dono
    ko ek hi merged list mein dikhata hai, sirf `source` field se pata
    chalta hai ye project deliverable hai ya task attachment."""

    id = serializers.SerializerMethodField()
    projectId = serializers.IntegerField(source="id", read_only=True)
    projectName = serializers.CharField(source="name", read_only=True)
    projectDetails = serializers.CharField(source="description", read_only=True)
    client = serializers.CharField(source="client.name", read_only=True, default="")
    fileName = serializers.CharField(source="zip_original_name", read_only=True)
    uploadedBy = serializers.CharField(source="zip_uploaded_by.name", read_only=True, default="")
    uploadedOn = serializers.DateTimeField(source="zip_uploaded_at", read_only=True)
    # Frontend MB mein dikhata hai (f.size.toFixed(1)) — yahin convert
    # kar dete hain taake frontend ko koi maths na karni pade.
    size = serializers.SerializerMethodField()
    downloadUrl = serializers.SerializerMethodField()
    deleteUrl = serializers.SerializerMethodField()
    source = serializers.SerializerMethodField()

    class Meta:
        model = Project
        fields = [
            "id", "projectId", "projectName", "client", "projectDetails", "fileName",
            "uploadedBy", "uploadedOn", "size", "downloadUrl", "deleteUrl", "source",
        ]

    def get_id(self, obj):
        return f"project-{obj.id}"

    def get_size(self, obj):
        return round((obj.zip_size or 0) / (1024 * 1024), 2)

    def get_downloadUrl(self, obj):
        request = self.context.get("request")
        path = f"/api/projects/{obj.id}/zip/download/"
        return request.build_absolute_uri(path) if request else path

    def get_deleteUrl(self, obj):
        request = self.context.get("request")
        path = f"/api/projects/{obj.id}/zip/"
        return request.build_absolute_uri(path) if request else path

    def get_source(self, obj):
        return "project"


class ModuleFileSerializer(serializers.ModelSerializer):
    uploaded_by_name = serializers.CharField(source="uploaded_by.name", read_only=True)
    approved_by_name = serializers.CharField(source="approved_by.name", read_only=True, default="")

    class Meta:
        model = ModuleFile
        fields = [
            "id", "file", "original_name", "mime_type", "size", "uploaded_by", "uploaded_by_name", "uploaded_at",
            "approved", "approved_by", "approved_by_name", "approved_at",
        ]
        read_only_fields = ["uploaded_by", "mime_type", "size", "original_name", "approved_by", "approved_at"]


class ModuleSubtaskSerializer(serializers.ModelSerializer):
    """One level of nesting only — a sub-task (e.g. Development's
    Frontend/Backend/...) never has sub-tasks of its own, so this stays
    a plain (non-recursive) serializer rather than ModuleSerializer
    nesting itself."""

    files = ModuleFileSerializer(many=True, read_only=True)
    assignee_name = serializers.CharField(source="assignee.name", read_only=True, default="")

    class Meta:
        model = Module
        fields = [
            "id", "project", "parent", "name", "assignee", "assignee_name", "status",
            "priority", "due_date", "url", "url_approved", "price", "files", "unlocked",
        ]
        # url_approved can ONLY be flipped through the dedicated approve-url
        # endpoint — never through a plain PATCH from the Task Page.
        read_only_fields = ["project", "url_approved"]


class ModuleSerializer(serializers.ModelSerializer):
    files = ModuleFileSerializer(many=True, read_only=True)
    assignee_name = serializers.CharField(source="assignee.name", read_only=True, default="")
    # NEW — real backend rows for ClientsPage.jsx's "sub-modules" (see
    # Module.parent in models.py). Empty for an ordinary module.
    subtasks = ModuleSubtaskSerializer(many=True, read_only=True)

    class Meta:
        model = Module
        fields = [
            "id", "project", "parent", "name", "assignee", "assignee_name", "status",
            "priority", "due_date", "url", "url_approved", "price", "files", "unlocked", "subtasks",
        ]
        read_only_fields = ["project", "url_approved"]

    def validate_price(self, value):
        # Sirf admin hi price set/dekh sakta hai — frontend ke
        # `isAdmin && ...` price guards jaisa hi.
        request = self.context.get("request")
        if request and request.user.role != "admin" and value:
            raise serializers.ValidationError("Only an admin can set module price.")
        return value


class ProjectListSerializer(serializers.ModelSerializer):
    # source="top_level_modules.count": a sub-task shouldn't inflate the
    # module-count badge on the Projects list — it's counted once, under
    # its parent, the same way ClientsPage.jsx's own moduleCount already
    # treats "Development" as ONE module regardless of its sub-tasks.
    module_count = serializers.IntegerField(source="top_level_modules.count", read_only=True)
    client_name = serializers.CharField(source="client.name", read_only=True, default="")
    manager_name = serializers.CharField(source="manager.name", read_only=True, default="")
    # FIX (modules disappearing every time the Projects page is reloaded /
    # navigated back to): this list response used to carry ONLY
    # module_count — the real modules (name, assignee, status, price,
    # files, ...) were never included here at all. ProjectsPage.jsx's
    # loadFromBackend() calls exactly this LIST endpoint on every mount,
    # and falls back to an empty modules array whenever `modules` isn't
    # present on the response — so a module added (and correctly saved
    # on the backend, assignee and all) would vanish from the UI the
    # moment the person switched pages and came back, purely because
    # this serializer never sent it back. Same shape/source as
    # ProjectDetailSerializer.modules so both endpoints agree.
    modules = ModuleSerializer(many=True, read_only=True, source="top_level_modules")

    class Meta:
        model = Project
        fields = [
            "id", "name", "description", "project_type", "client", "client_name",
            "manager", "manager_name", "team", "status", "priority", "budget", "spent",
            "start_date", "due_date", "module_count", "modules", "is_archived",
            "features", "requirements", "additional_info", "completion_link",
            "created_at", "updated_at",
        ]


class ProjectDetailSerializer(serializers.ModelSerializer):
    # source="top_level_modules": sub-tasks nest inside their parent's
    # own `subtasks` (see ModuleSerializer) instead of also appearing
    # here a second time as a flat duplicate entry.
    modules = ModuleSerializer(many=True, read_only=True, source="top_level_modules")
    client_name = serializers.CharField(source="client.name", read_only=True, default="")
    manager_name = serializers.CharField(source="manager.name", read_only=True, default="")

    class Meta:
        model = Project
        fields = [
            "id", "name", "description", "project_type", "client", "client_name",
            "manager", "manager_name", "team", "status", "priority", "budget", "spent",
            "start_date", "due_date", "brief", "brief_uploaded_by",
            "completed_zip", "zip_uploaded_by", "modules",
            "features", "requirements", "additional_info", "completion_link",
            "created_by", "is_archived", "created_at", "updated_at",
        ]
        read_only_fields = ["brief", "completed_zip", "created_by", "is_archived"]

    def validate(self, attrs):
        request = self.context.get("request")
        if request and request.user.role != "admin":
            attrs.pop("budget", None)
            attrs.pop("spent", None)
        return attrs