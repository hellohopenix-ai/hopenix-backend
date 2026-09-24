from django.shortcuts import get_object_or_404
from django.http import FileResponse, Http404
from django.utils import timezone
from rest_framework import viewsets, permissions, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from rest_framework.exceptions import PermissionDenied, ValidationError

from .models import Project, Module, ModuleFile
from .serializers import (
    ProjectListSerializer,
    ProjectDetailSerializer,
    ProjectZipSerializer,
    ModuleSerializer,
    ModuleFileSerializer,
)
from .permissions import (
    visible_projects_queryset,
    can_access_project,
    can_manage_project,
    can_approve_project_content,
    can_access_file,
    ProjectObjectPermission,
)
from .utils import build_project_zip


class ProjectViewSet(viewsets.ModelViewSet):
    permission_classes = [permissions.IsAuthenticated, ProjectObjectPermission]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get_queryset(self):
        # select_related/prefetch_related: ProjectListSerializer now also
        # serializes each project's modules (see serializers.py FIX), so
        # without this every project in the list would re-query its
        # modules, their subtasks, files and assignees one by one.
        base = Project.objects.select_related("client", "manager").prefetch_related(
            "modules__assignee", "modules__subtasks__assignee", "modules__subtasks__files", "modules__files",
        )
        qs = visible_projects_queryset(self.request.user, base)
        # BUG FIX: ClientsPage.jsx's listClientProjects(clientId) has always
        # sent ?client=<id> expecting it to scope the list to that one
        # client (same pattern dashboard.ClientViewSet/InvoiceViewSet/
        # ModuleRequestViewSet already follow) — but this view never read
        # the query param at all, so it silently returned every project
        # the requesting user could see instead. Same fix pattern applied
        # for ?manager= for consistency with how the rest of the API
        # already supports it.
        p = self.request.query_params
        client_id = p.get("client")
        if client_id:
            qs = qs.filter(client_id=client_id)
        manager_id = p.get("manager")
        if manager_id:
            qs = qs.filter(manager_id=manager_id)
        return qs

    def get_serializer_class(self):
        return ProjectListSerializer if self.action == "list" else ProjectDetailSerializer

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

    def destroy(self, request, *args, **kwargs):
        # Sirf admin, aur hard-delete nahi — soft delete (is_archived).
        project = self.get_object()
        if request.user.role != "admin":
            return Response({"error": "Only admin can delete projects."}, status=status.HTTP_403_FORBIDDEN)
        project.is_archived = True
        project.save(update_fields=["is_archived"])
        return Response({"message": "Project archived."})

    @action(detail=True, methods=["post"], url_path="restore")
    def restore(self, request, pk=None):
        if request.user.role != "admin":
            return Response({"error": "Only admin can restore projects."}, status=status.HTTP_403_FORBIDDEN)
        project = get_object_or_404(Project, pk=pk)
        project.is_archived = False
        project.save(update_fields=["is_archived"])
        return Response(ProjectDetailSerializer(project).data)

    # -- Brief (pdf/image) --------------------------------------------
    @action(detail=True, methods=["post"], url_path="brief")
    def upload_brief(self, request, pk=None):
        project = self.get_object()
        if not can_manage_project(request.user, project):
            return Response({"error": "Not authorized."}, status=403)

        file_obj = request.FILES.get("brief")
        if not file_obj:
            return Response({"error": "No file uploaded."}, status=400)
        if file_obj.content_type != "application/pdf" and not file_obj.content_type.startswith("image/"):
            return Response({"error": "Brief must be a PDF or an image."}, status=400)

        if project.brief:
            project.brief.delete(save=False)

        project.brief = file_obj
        project.brief_uploaded_by = request.user
        project.save(update_fields=["brief", "brief_uploaded_by"])
        return Response(ProjectDetailSerializer(project).data, status=201)

    @action(detail=True, methods=["get"], url_path="brief/download")
    def download_brief(self, request, pk=None):
        project = self.get_object()
        if not can_access_project(request.user, project):
            return Response({"error": "Not authorized."}, status=403)
        if not project.brief:
            raise Http404("No brief uploaded.")
        return FileResponse(project.brief.open("rb"), as_attachment=True, filename=project.brief.name.split("/")[-1])

    # -- Completed deliverable zip --------------------------------------
    @action(detail=True, methods=["post", "delete"], url_path="zip")
    def upload_zip(self, request, pk=None):
        project = self.get_object()
        if not can_manage_project(request.user, project):
            return Response({"error": "Not authorized."}, status=403)

        # DELETE /api/projects/<id>/zip/ — Zip Files page ke "Delete"
        # button ke liye. File disk se bhi hatti hai aur saare metadata
        # fields bhi clear ho jaate hain (purana localStorage wala
        # handleDelete isi kaam ka backend version hai).
        if request.method == "DELETE":
            if not project.completed_zip:
                return Response({"error": "No deliverable zip to delete."}, status=404)
            file_name = project.zip_original_name
            project.completed_zip.delete(save=False)
            project.zip_uploaded_by = None
            project.zip_original_name = ""
            project.zip_size = 0
            project.zip_uploaded_at = None
            project.save(update_fields=[
                "completed_zip", "zip_uploaded_by", "zip_original_name", "zip_size", "zip_uploaded_at",
            ])
            return Response({"message": f"{file_name or 'Zip file'} deleted."})

        file_obj = request.FILES.get("zip")
        if not file_obj:
            return Response({"error": "No zip uploaded."}, status=400)
        if not file_obj.name.lower().endswith(".zip"):
            return Response({"error": "Only .zip files are allowed."}, status=400)

        if project.completed_zip:
            project.completed_zip.delete(save=False)

        project.completed_zip = file_obj
        project.zip_uploaded_by = request.user
        # Ye teeno Zip Files page ki list ke liye save ho rahe hain — na
        # to har list-request par disk se size dobara nikalni padti hai,
        # na hi original naam FileField ke uuid-prefixed storage path se
        # dobara nikalna padta hai.
        project.zip_original_name = file_obj.name
        project.zip_size = file_obj.size
        project.zip_uploaded_at = timezone.now()
        project.save(update_fields=[
            "completed_zip", "zip_uploaded_by", "zip_original_name", "zip_size", "zip_uploaded_at",
        ])
        # FIX: pass request context so `completed_zip` serializes to an
        # absolute, directly-usable URL (http://host/media/...) instead
        # of a bare relative media path — this response is what
        # clientsApi.uploadProjectZip()'s caller uses to build the zip
        # metadata object it stores/displays immediately, without waiting
        # on a full page refetch.
        return Response(ProjectDetailSerializer(project, context={"request": request}).data, status=201)

    @action(detail=True, methods=["get"], url_path="zip/download")
    def download_zip(self, request, pk=None):
        project = self.get_object()
        if not can_access_project(request.user, project):
            return Response({"error": "Not authorized."}, status=403)
        if not project.completed_zip:
            raise Http404("No deliverable zip uploaded yet.")
        return FileResponse(
            project.completed_zip.open("rb"),
            as_attachment=True,
            filename=project.zip_original_name or project.completed_zip.name.split("/")[-1],
        )

    @action(detail=True, methods=["get"], url_path="export-zip")
    def export_zip(self, request, pk=None):
        project = self.get_object()
        if not can_access_project(request.user, project):
            return Response({"error": "Not authorized."}, status=403)
        return build_project_zip(project)

    # -- Zip Files page (admin-ish list across ALL projects + tasks) ----
    @action(detail=False, methods=["get"], url_path="zip-files")
    def zip_files(self, request):
        """GET /api/projects/zip-files/
        ZipFilesPage.jsx ke liye ek hi endpoint — do sources merge karke:
          1) har (visible) project ki completed_zip (ProjectZipSerializer)
          2) har task par attach ki hui .zip file (TaskZipFileSerializer,
             tasks app se) — cross-app import hai, koi circular problem
             nahi kyunki tasks app projects ko import nahi karta.
        Visibility: project side wahi rule follow karti hai jo baaki
        project list mein hai (visible_projects_queryset). Task side
        TaskViewSet jaisi hi hai — abhi tasks par koi per-user
        restriction nahi (sirf login required), to zip attachments bhi
        usi tarah sab logged-in users ko dikhti hain."""
        from tasks.models import TaskZipFile
        from tasks.serializers import TaskZipFileSerializer

        project_qs = visible_projects_queryset(request.user, Project.objects.all()).exclude(completed_zip="")
        project_rows = ProjectZipSerializer(project_qs, many=True, context={"request": request}).data

        task_qs = TaskZipFile.objects.select_related("task", "task__client", "uploaded_by")
        task_rows = TaskZipFileSerializer(task_qs, many=True, context={"request": request}).data

        merged = list(project_rows) + list(task_rows)
        merged.sort(key=lambda row: row.get("uploadedOn") or "", reverse=True)
        return Response(merged)


class ModuleViewSet(viewsets.ModelViewSet):
    """Nested: /api/projects/<project_pk>/modules/"""

    serializer_class = ModuleSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_project(self):
        project = get_object_or_404(Project, pk=self.kwargs["project_pk"], is_archived=False)
        if not can_access_project(self.request.user, project):
            raise PermissionDenied("Not authorized to view this project.")
        return project

    def get_queryset(self):
        return Module.objects.filter(project=self.get_project())

    def perform_create(self, serializer):
        project = self.get_project()
        if not can_manage_project(self.request.user, project):
            raise PermissionDenied("Not authorized to modify this project.")
        module = serializer.save(project=project)
        self._sync_module_task(module)

    # FIX (Add Client -> Task page never got the new module): creating a
    # Module here (e.g. ClientsPage.jsx's syncClientProjectToBackend,
    # fired right after a new client/project is added) used to only ever
    # create the real Postgres Module row. The matching Task row
    # TasksPage.jsx actually reads from was only ever generated
    # client-side, from a localStorage snapshot of the Clients page — and
    # only if/when someone happened to open the Tasks page in that same
    # browser after the fire-and-forget project sync had finished. On a
    # different browser/device, or before that timing lined up, GET
    # /tasks/ never had the row at all, so it looked like the module was
    # never "assigned" on the Tasks page.
    #
    # This mirrors ModuleRequestViewSet.accept's own
    # Task.objects.get_or_create, using the exact same module_task_key
    # shape TasksPage.jsx's own moduleTaskKey() builds (client id +
    # PROJECT NAME + module id [+ sub-module id]) so the frontend's own
    # client-side sync recognises this row as already-linked and never
    # creates a duplicate for it. Every module now gets a real Task the
    # moment it exists — locked or not — without depending on any
    # browser's storage to eventually create it.
    def _sync_module_task(self, module):
        if not module.project.client_id:
            return  # internal/company projects were never fed into the module-task engine
        from tasks.models import Task
        from dashboard.views import LINK_REQUIRED_MODULES

        client_id = module.project.client_id
        project_name = module.project.name
        status_ = "Completed" if module.status == "Completed" else "Pending"

        if module.parent_id:
            # A sub-task (e.g. Development's Frontend/Backend/...) is its
            # own leaf, keyed under the PARENT's id + this row's own id —
            # matches moduleLeaves()'s subModuleId handling exactly. Never
            # lock-gated, same as the frontend treats it.
            Task.objects.get_or_create(
                module_task_key=f"{client_id}::{project_name}::{module.parent_id}::{module.id}",
                defaults=dict(
                    title=f"{module.parent.name}: {module.name}",
                    project=project_name,
                    client_id=client_id,
                    status=status_,
                    locked=False,
                    module_name=module.name,
                    module_project_name=project_name,
                    from_client_module=True,
                    requires_link=module.name in LINK_REQUIRED_MODULES,
                    module=module,
                ),
            )
            # This module no longer stands as its own leaf now that it has
            # a sub-task under it — drop whatever task got auto-created
            # for it when IT was first created (before this child
            # existed), so it doesn't sit on the Tasks page as a phantom
            # duplicate of its own children.
            Task.objects.filter(module_task_key=f"{client_id}::{project_name}::{module.parent_id}::").delete()
        else:
            Task.objects.get_or_create(
                module_task_key=f"{client_id}::{project_name}::{module.id}::",
                defaults=dict(
                    title=module.name,
                    project=project_name,
                    client_id=client_id,
                    status=status_,
                    locked=not module.unlocked,
                    module_name=module.name,
                    module_project_name=project_name,
                    from_client_module=True,
                    requires_link=module.name in LINK_REQUIRED_MODULES,
                    module=module,
                ),
            )

    def perform_update(self, serializer):
        project = self.get_project()
        if not can_manage_project(self.request.user, project):
            raise PermissionDenied("Not authorized to modify this project.")
        # FIX (link reached the Client Portal without admin approval): a
        # changed/new link goes back to "pending review" — otherwise an
        # employee could swap an already-approved URL for a different one
        # and it would stay live on the Portal unreviewed.
        instance = serializer.instance
        extra = {}
        if serializer.validated_data.get("url", instance.url) != instance.url:
            extra = dict(url_approved=False, url_approved_by=None, url_approved_at=None)
        serializer.save(**extra)

    @action(detail=True, methods=["post"], url_path="approve-url")
    def approve_url(self, request, project_pk=None, pk=None):
        """POST body: {"approved": true|false}. Admin / project manager only.
        Link counterpart of ModuleFileViewSet.approve — the ONLY thing that
        makes a module's live/staging link visible to the Client Portal (see
        dashboard/serializers.py's _module_attachments)."""
        project = self.get_project()
        if not can_approve_project_content(request.user, project):
            return Response({"error": "Only an admin or the project manager can approve."}, status=403)

        module = self.get_object()
        if not module.url:
            return Response({"error": "This module has no link to approve."}, status=400)

        approved = request.data.get("approved", True)
        if isinstance(approved, str):
            approved = approved.strip().lower() not in ("false", "0", "no", "")
        approved = bool(approved)

        module.url_approved = approved
        module.url_approved_by = request.user if approved else None
        module.url_approved_at = timezone.now() if approved else None
        module.save(update_fields=["url_approved", "url_approved_by", "url_approved_at", "updated_at"])
        return Response(ModuleSerializer(module, context={"request": request}).data)

    def perform_destroy(self, instance):
        project = self.get_project()
        if not can_manage_project(self.request.user, project):
            raise PermissionDenied("Not authorized to modify this project.")
        instance.delete()


class ModuleFileViewSet(viewsets.ModelViewSet):
    """Nested: /api/projects/<project_pk>/modules/<module_pk>/files/"""

    serializer_class = ModuleFileSerializer
    permission_classes = [permissions.IsAuthenticated]
    # JSONParser added: the approve action's body is JSON ({"approved": true}),
    # and with only the two multipart parsers DRF answered 415 to it.
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get_module(self):
        return get_object_or_404(Module, pk=self.kwargs["module_pk"], project_id=self.kwargs["project_pk"])

    def get_project(self):
        return get_object_or_404(Project, pk=self.kwargs["project_pk"], is_archived=False)

    def get_queryset(self):
        return ModuleFile.objects.filter(module=self.get_module())

    def perform_create(self, serializer):
        project = self.get_project()
        if not can_manage_project(self.request.user, project):
            raise PermissionDenied("Not authorized to modify this project.")

        file_obj = self.request.FILES.get("file")
        if not file_obj:
            raise ValidationError("No file uploaded.")

        serializer.save(
            module=self.get_module(),
            file=file_obj,
            original_name=file_obj.name,
            mime_type=file_obj.content_type or "",
            size=file_obj.size,
            uploaded_by=self.request.user,
        )

    def perform_destroy(self, instance):
        project = self.get_project()
        if not can_manage_project(self.request.user, project):
            raise PermissionDenied("Not authorized to modify this project.")
        instance.file.delete(save=False)
        instance.delete()

    @action(detail=True, methods=["get"], url_path="download")
    def download(self, request, project_pk=None, module_pk=None, pk=None):
        project = get_object_or_404(Project, pk=project_pk, is_archived=False)
        module_file = get_object_or_404(ModuleFile, pk=pk, module_id=module_pk)

        if not can_access_file(request.user, project, module_file):
            return Response({"error": "Not authorized to view this file."}, status=403)

        return FileResponse(module_file.file.open("rb"), as_attachment=True, filename=module_file.original_name)

    # -- Review / approval gate (Client Page's "Approved / Show on Portal"
    # checkbox beside a submitted file) --------------------------------
    @action(detail=True, methods=["post"], url_path="approve")
    def approve(self, request, project_pk=None, module_pk=None, pk=None):
        """POST body: {"approved": true|false}. Admin / project manager only. This is the
        ONLY thing that can make a Task Page upload visible to the Client
        Portal — see dashboard/serializers.py's _module_attachments,
        which now hides any ModuleFile with approved=False from a client
        viewer regardless of the module's own lock/unlock state."""
        project = self.get_project()
        # Admin / project manager only — NOT every team member, otherwise the
        # uploader could approve their own file straight onto the Portal.
        if not can_approve_project_content(request.user, project):
            return Response({"error": "Only an admin or the project manager can approve."}, status=403)

        module_file = get_object_or_404(ModuleFile, pk=pk, module_id=module_pk)
        approved = request.data.get("approved", True)
        if isinstance(approved, str):
            approved = approved.strip().lower() not in ("false", "0", "no", "")
        approved = bool(approved)
        module_file.approved = approved
        module_file.approved_by = request.user if approved else None
        module_file.approved_at = timezone.now() if approved else None
        module_file.save(update_fields=["approved", "approved_by", "approved_at"])
        return Response(ModuleFileSerializer(module_file, context={"request": request}).data)