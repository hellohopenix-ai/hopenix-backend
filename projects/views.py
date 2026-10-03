import logging

from django.core.files.base import ContentFile
from django.shortcuts import get_object_or_404
from django.http import FileResponse, Http404
from datetime import timedelta

from django.db.models import Q
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
from messaging.push_utils import notify_module_assigned, notify_project_assigned

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module -> Task mirror for FILES, ZIPs and the module LINK.
#
# Tasks created from a module (Task.module) used to learn about the module's
# status and assignee (see ModuleViewSet._mirror_module_to_tasks) but NOT about
# files / zips / the link attached to the module from the Projects page — so
# they showed up on the Clients page (which reads the Module directly) but
# never on the Tasks page / Zip Files page. The Task -> Module direction
# already existed (TaskViewSet.upload_file_attachment / upload_zip / complete),
# this is the missing reverse direction.
#
# NO-OVERRIDE RULE (same as the status mirror): only the one file / link that
# really changed is added or removed, everything else on the task is kept, and
# queryset.update() is used on purpose so TaskViewSet.perform_update is not
# triggered and the two sides can never ping-pong. These helpers are also only
# called from the Projects-side viewsets — the Task-side endpoints create their
# ModuleFile rows through the ORM directly, so they never re-enter here.
# ---------------------------------------------------------------------------
def _file_kind(name, mime):
    mime = (mime or "").lower()
    lower = (name or "").lower()
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("video/"):
        return "video"
    if lower.endswith(".zip") or mime in ("application/zip", "application/x-zip-compressed"):
        return "zip"
    return "file"


def _norm_url(u):
    return str(u or "").strip().rstrip("/")


def _tasks_for_module(module):
    """Every Task that belongs to `module`.

    FIX (tick / file / zip / link never reached the Tasks page): all the
    Module -> Task mirrors below used Task.objects.filter(module=module), but
    Task.module is empty for any task whose module was assigned from the
    Projects page (and for older tasks), so those tasks were silently skipped.
    Such orphan tasks are matched by project + module name and ADOPTED (their
    FK is stamped) - but only when that module name is unique inside the
    project, so a wrong task is never linked."""
    from tasks.models import Task

    project = module.project
    orphans = Task.objects.filter(module__isnull=True, module_name=module.name)
    if project.client_id:
        orphans = orphans.filter(client_id=project.client_id).filter(
            Q(module_project_name=project.name) | Q(project=project.name)
        )
    else:
        orphans = orphans.filter(project=project.name)
    if Module.objects.filter(project=project, name=module.name).count() == 1:
        orphan_ids = list(orphans.values_list("pk", flat=True))
        if orphan_ids:
            Task.objects.filter(pk__in=orphan_ids).update(module=module)
    return Task.objects.filter(module=module)


def mirror_module_file_to_tasks(module_file, request):
    from tasks.models import Task, TaskZipFile

    now = timezone.now()
    kind = _file_kind(module_file.original_name, module_file.mime_type)
    for task in _tasks_for_module(module_file.module):
        entries = list(task.attachments or [])
        if any(
            isinstance(e, dict)
            and (str(e.get("id", "")) == str(module_file.id) or e.get("moduleFileId") == module_file.id)
            for e in entries
        ):
            continue  # already mirrored

        entry = None
        if kind == "zip":
            # Same as TaskViewSet.upload_zip: a real TaskZipFile, so the
            # Tasks page can download it and the Zip Files page lists it.
            try:
                module_file.file.open("rb")
                try:
                    data = module_file.file.read()
                finally:
                    module_file.file.close()
                zip_row = TaskZipFile.objects.create(
                    task=task,
                    file=ContentFile(data, name=module_file.original_name),
                    original_name=module_file.original_name,
                    size=module_file.size or len(data),
                    uploaded_by=request.user,
                )
                entry = {
                    "type": "zip",
                    "name": module_file.original_name,
                    "zipFileId": zip_row.id,
                    "moduleFileId": module_file.id,
                    "addedAt": now.isoformat(),
                }
            except Exception:  # noqa: BLE001
                logger.exception("Module zip -> task zip mirror failed (module file %s)", module_file.id)
        if entry is None:
            entry = {
                "id": module_file.id,
                "type": "file" if kind == "zip" else kind,
                "name": module_file.original_name,
                "url": request.build_absolute_uri(module_file.file.url),
                "addedAt": now.isoformat(),
            }
        Task.objects.filter(pk=task.pk).update(attachments=[*entries, entry], updated_at=now)


def unmirror_module_file_from_tasks(module_file):
    """Deleting a file from the Projects page removes it from the module's
    tasks too (and deletes the TaskZipFile copy made for a zip)."""
    from tasks.models import Task, TaskZipFile

    now = timezone.now()
    is_zip_name = (module_file.original_name or "").lower().endswith(".zip")
    for task in _tasks_for_module(module_file.module):
        keep, changed = [], False
        for e in task.attachments or []:
            if not isinstance(e, dict):
                keep.append(e)
                continue
            same = str(e.get("id", "")) == str(module_file.id) or e.get("moduleFileId") == module_file.id
            same_zip = (
                is_zip_name
                and e.get("type") == "zip"
                and e.get("zipFileId") not in (None, "")
                and e.get("name") == module_file.original_name
            )
            if not (same or same_zip):
                keep.append(e)
                continue
            changed = True
            zid = str(e.get("zipFileId", "")).replace("task-", "", 1)
            if zid.isdigit():
                zip_row = TaskZipFile.objects.filter(pk=int(zid), task=task).first()
                if zip_row:
                    zip_row.file.delete(save=False)
                    zip_row.delete()
        if changed:
            Task.objects.filter(pk=task.pk).update(attachments=keep, updated_at=now)


def mirror_module_url_to_tasks(module, old_url):
    """Module.url is ONE link (last one wins). Keep the module's tasks in
    step: the old link entry is swapped for the new one, or removed when the
    link was cleared."""
    from tasks.models import Task

    new_url = (module.url or "").strip()
    old_url = (old_url or "").strip()
    if _norm_url(new_url) == _norm_url(old_url):
        return
    now = timezone.now()
    for task in _tasks_for_module(module):
        entries = [
            e for e in (task.attachments or [])
            if not (
                isinstance(e, dict) and e.get("type") == "link" and old_url
                and _norm_url(e.get("url")) == _norm_url(old_url)
            )
        ]
        if new_url and not any(
            isinstance(e, dict) and e.get("type") == "link" and _norm_url(e.get("url")) == _norm_url(new_url)
            for e in entries
        ):
            entries.append({
                "id": f"modlink-{module.id}-{int(now.timestamp())}",
                "type": "link",
                "name": new_url,
                "url": new_url,
                "uploadedAt": now.isoformat(),
                "addedAt": now.isoformat(),
            })
        fields = {"attachments": entries, "updated_at": now}
        if new_url:
            fields["attachment"] = new_url
        elif _norm_url(task.attachment) == _norm_url(old_url):
            fields["attachment"] = ""
        Task.objects.filter(pk=task.pk).update(**fields)



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

    def create(self, request, *args, **kwargs):
        # Double-click / retry guard: the same person creating a project
        # with the same name within a few seconds is the same click sent
        # twice, not a second project. Return the one already saved
        # instead of creating 2-3 copies (each of which also used to get
        # its own commission, so pay was counted several times).
        name = str((request.data.get("name") if hasattr(request.data, "get") else "") or "").strip()
        if name:
            recent = (
                Project.objects.filter(
                    created_by=request.user,
                    name__iexact=name,
                    is_archived=False,
                    created_at__gte=timezone.now() - timedelta(seconds=20),
                )
                .order_by("-created_at")
                .first()
            )
            if recent is not None:
                return Response(
                    ProjectDetailSerializer(recent, context=self.get_serializer_context()).data,
                    status=status.HTTP_200_OK,
                )
        return super().create(request, *args, **kwargs)

    def perform_create(self, serializer):
        project = serializer.save(created_by=self.request.user)
        self._notify_project_people(project, set())

    def perform_update(self, serializer):
        old = serializer.instance
        old_ids = set(old.team.values_list("id", flat=True))
        if old.manager_id:
            old_ids.add(old.manager_id)
        project = serializer.save()
        self._notify_project_people(project, old_ids)

    def _notify_project_people(self, project, already_ids):
        """OS + in-app notification for everyone newly added to the project
        (manager or team). Never allowed to fail the save."""
        try:
            people = list(project.team.all())
            if project.manager_id:
                people.append(project.manager)
            new_people = [u for u in people if u.id not in already_ids]
            if new_people:
                notify_project_assigned(project, new_people, self.request.user)
        except Exception:  # noqa: BLE001
            logger.exception("Project-assignment notification failed")

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
        if module.url:
            try:
                mirror_module_url_to_tasks(module, "")
            except Exception:  # noqa: BLE001
                logger.exception("Module link -> task mirror failed")
        self._notify_assignee(module)

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
        old_url = instance.url
        old_status = instance.status
        old_assignee = instance.assignee
        old_assignee_name = getattr(old_assignee, "name", "") or ""
        extra = {}
        if serializer.validated_data.get("url", instance.url) != instance.url:
            extra = dict(url_approved=False, url_approved_by=None, url_approved_at=None)
        module = serializer.save(**extra)
        self._mirror_module_to_tasks(module, old_status, old_assignee_name)
        if module.status != old_status:
            from .handoff import handle_module_status_change

            handle_module_status_change(module, old_status, self.request.user, self.request)
        if (module.url or "").strip() and module.url != old_url:
            from .handoff import notify_new_link

            notify_new_link(module, module.url, self.request.user, self.request)
        try:
            mirror_module_url_to_tasks(module, old_url)
        except Exception:  # noqa: BLE001
            logger.exception("Module link -> task mirror failed")
        if module.assignee_id and module.assignee_id != getattr(old_assignee, "id", None):
            self._notify_assignee(module)

    @action(detail=True, methods=["post"], url_path="approve-handoff")
    def approve_handoff(self, request, project_pk=None, pk=None):
        """POST /api/projects/<project>/modules/<module>/approve-handoff/
        Admin only. Forwards the finished module's link/files/zips (which
        were sent to the admin when it was completed) to the next member."""
        if request.user.role != "admin":
            return Response({"error": "Only an admin can approve this."}, status=403)
        from .handoff import approve_handoff as _approve

        module = self.get_object()
        ok, message = _approve(module, request.user, request)
        if not ok:
            return Response({"error": message}, status=400)
        module.refresh_from_db()
        data = ModuleSerializer(module, context={"request": request}).data
        data["message"] = message
        return Response(data)

    def _notify_assignee(self, module):
        """Sidebar dot + OS notification for the (new) module assignee.
        Never allowed to fail the save."""
        if not module.assignee_id:
            return
        try:
            notify_module_assigned(module, self.request.user)
        except Exception:  # noqa: BLE001
            logging.getLogger(__name__).exception("Module-assignment notification failed")
        # Text + project brief PDF go to the new assignee's chat automatically.
        from .handoff import send_assignment_brief

        send_assignment_brief(module, self.request.user, self.request)

    # FIX (module ticked on the Projects page never reached the Tasks page):
    # the Task -> Module direction already existed (TaskViewSet.
    # _sync_linked_module_status) but nothing pushed a Module change back to
    # its Task, so a tick / status change / new assignee made from the
    # Projects page stayed invisible on the Tasks page on every other device.
    #
    # NO-OVERRIDE RULE: only the field that really changed in THIS update is
    # mirrored (status only if status changed, assignee only if the assignee
    # changed), and the task's other assignees are kept. A plain edit of e.g.
    # the module's priority can therefore never reset a task's status or wipe
    # the group of people someone added on the Tasks page. queryset.update()
    # is used on purpose so TaskViewSet.perform_update is not triggered and
    # the two sides can never ping-pong.
    def _mirror_module_to_tasks(self, module, old_status, old_assignee_name):
        from tasks.models import Task

        tasks = _tasks_for_module(module)
        if not tasks.exists():
            return

        if module.status != old_status:
            if module.status == "Completed":
                tasks.exclude(status="Completed").update(status="Completed", progress=100)
            elif module.status == "In Progress":
                # progress must be > 0, otherwise the Task -> Module sync
                # would read this task as "Pending" again.
                tasks.exclude(status="In Progress", progress__gt=0, progress__lt=100).update(
                    status="In Progress", progress=50
                )
            else:  # Pending (also "un-tick" of a completed module)
                tasks.exclude(status="Pending", progress=0).update(status="Pending", progress=0)

        new_assignee_name = getattr(module.assignee, "name", "") or ""
        if new_assignee_name != old_assignee_name:
            for task in tasks:
                names = [n for n in (task.assignees or []) if n and n != old_assignee_name]
                if new_assignee_name and new_assignee_name not in names:
                    names.insert(0, new_assignee_name)
                names = names[:3]  # a task holds at most 3 assignees
                if names != (task.assignees or []):
                    Task.objects.filter(pk=task.pk).update(assignees=names)

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

    def create(self, request, *args, **kwargs):
        # FIX (one attach -> two files): the same upload arriving twice (double
        # click / a retried request / two tabs) used to create two rows. An
        # identical file (same module, name, size, uploader) uploaded within the
        # last 15 seconds is treated as that same upload and returned as is.
        incoming = request.FILES.get("file")
        if incoming is not None:
            twin = ModuleFile.objects.filter(
                module_id=self.kwargs["module_pk"],
                original_name=incoming.name,
                size=incoming.size,
                uploaded_by=request.user,
                uploaded_at__gte=timezone.now() - timedelta(seconds=15),
            ).first()
            if twin is not None:
                return Response(self.get_serializer(twin).data, status=status.HTTP_200_OK)
        return super().create(request, *args, **kwargs)

    def perform_create(self, serializer):
        project = self.get_project()
        if not can_manage_project(self.request.user, project):
            raise PermissionDenied("Not authorized to modify this project.")

        file_obj = self.request.FILES.get("file")
        if not file_obj:
            raise ValidationError("No file uploaded.")

        module_file = serializer.save(
            module=self.get_module(),
            file=file_obj,
            original_name=file_obj.name,
            mime_type=file_obj.content_type or "",
            size=file_obj.size,
            uploaded_by=self.request.user,
        )
        try:
            mirror_module_file_to_tasks(module_file, self.request)
        except Exception:  # noqa: BLE001
            logger.exception("Module file -> task mirror failed")
        # Tell the admin about this new file - ONCE (see projects/handoff.py).
        from .handoff import notify_new_file

        notify_new_file(module_file, self.request.user, self.request)

    def perform_destroy(self, instance):
        project = self.get_project()
        if not can_manage_project(self.request.user, project):
            raise PermissionDenied("Not authorized to modify this project.")
        try:
            unmirror_module_file_from_tasks(instance)
        except Exception:  # noqa: BLE001
            logger.exception("Module file -> task un-mirror failed")
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
        was_approved = ModuleFile.objects.filter(pk=module_file.pk, approved=True).exists()
        module_file.approved = approved
        module_file.approved_by = request.user if approved else None
        module_file.approved_at = timezone.now() if approved else None
        module_file.save(update_fields=["approved", "approved_by", "approved_at"])
        if approved and not was_approved:
            # Newly visible on the Client Portal -> tell that project's client.
            try:
                from messaging.push_utils import notify_module_file_approved

                notify_module_file_approved(module_file)
            except Exception:  # noqa: BLE001 - never break approving because a push failed
                pass
        return Response(ModuleFileSerializer(module_file, context={"request": request}).data)