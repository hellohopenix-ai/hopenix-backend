from django.shortcuts import get_object_or_404
from django.http import FileResponse
from django.utils import timezone
from rest_framework import viewsets, permissions, status
from rest_framework.decorators import action
from rest_framework.response import Response

from projects.models import ModuleFile, Module, ModuleStatusChoices
from users.access import ModuleAccess

from .models import Task, TaskZipFile
from .serializers import (
    TaskSerializer,
    TaskBulkCreateItemSerializer,
    TaskCompleteSerializer,
    TaskAddAttachmentSerializer,
    TaskToggleSubtaskSerializer,
    TaskZipFileSerializer,
)


def _drop_zip_entries(task, zip_pk):
    """Remove every task.attachments entry that points at TaskZipFile <zip_pk>
    (the backend writes zipFileId as an int, the frontend keeps "task-<pk>")."""
    keys = {str(zip_pk), f"task-{zip_pk}"}
    task.attachments = [
        a for a in (task.attachments or [])
        if not (isinstance(a, dict) and str(a.get("zipFileId", "")) in keys)
    ]
    task.save(update_fields=["attachments", "updated_at"])


def _purge_task_zip(task, zip_file, module_id=None):
    """Delete one TaskZipFile completely and return the ids of the ModuleFile
    rows that mirrored it.

    upload_zip mirrors every zip into a ModuleFile that points at the SAME
    stored file (it is created from zip_file.file, already committed), so the
    bytes are removed exactly once, at the end. If no mirror shares the path
    (older data), fall back to the module's newest ModuleFile with the same
    name + size. `module_id` defaults to the task's own module."""
    deleted_ids = []
    stored_name = zip_file.file.name if zip_file.file else ""
    module_id = module_id or task.module_id
    if module_id:
        mirrors = []
        if stored_name:
            mirrors = list(ModuleFile.objects.filter(module_id=module_id, file=stored_name))
        if not mirrors:
            fallback = (
                ModuleFile.objects.filter(
                    module_id=module_id,
                    original_name=zip_file.original_name,
                    size=zip_file.size,
                )
                .order_by("-id")
                .first()
            )
            mirrors = [fallback] if fallback else []
        for mf in mirrors:
            deleted_ids.append(mf.id)
            if mf.file and mf.file.name != stored_name:
                mf.file.delete(save=False)  # its own bytes — the shared ones go below
            mf.delete()

    _clear_project_zip_if_matches(task, zip_file)
    _drop_zip_entries(task, zip_file.id)
    if stored_name:
        zip_file.file.delete(save=False)
    zip_file.delete()
    return deleted_ids


def _project_for_task(task):
    """The real projects.Project a task belongs to: through its linked
    Module when it has one, else by (client, project name)."""
    from projects.models import Project
    if task.module_id:
        return task.module.project
    if task.client_id and task.project:
        return Project.objects.filter(client_id=task.client_id, name=task.project).first()
    return None


def _mirror_final_zip_to_project(task, zip_file, user):
    """FIX (final zip uploaded from the Tasks page never showed on the
    Clients page / Client Portal): those pages read the project's own
    Project.completed_zip, but the Tasks page only ever created a
    TaskZipFile + a module attachment. A zip flagged as the FINAL
    deliverable is now also stored as the project's completed_zip. It is
    saved as its own COPY of the bytes, so deleting one never breaks the
    other. Best-effort — the TaskZipFile itself is already saved."""
    from django.core.files import File
    project = _project_for_task(task)
    if not project:
        return
    try:
        if project.completed_zip:
            project.completed_zip.delete(save=False)
        zip_file.file.open("rb")
        try:
            project.completed_zip.save(zip_file.original_name, File(zip_file.file), save=False)
        finally:
            zip_file.file.close()
        project.zip_uploaded_by = user
        project.zip_original_name = zip_file.original_name
        project.zip_size = zip_file.size
        project.zip_uploaded_at = timezone.now()
        project.save(update_fields=[
            "completed_zip", "zip_uploaded_by", "zip_original_name", "zip_size", "zip_uploaded_at",
        ])
    except Exception:
        pass


def _clear_project_zip_if_matches(task, zip_file):
    """When a final-deliverable zip is deleted from the Tasks page, drop
    the project's copy too (only if it is the same file)."""
    try:
        project = _project_for_task(task)
        if (
            project and project.completed_zip
            and project.zip_original_name == zip_file.original_name
            and project.zip_size == zip_file.size
        ):
            project.completed_zip.delete(save=False)
            project.zip_uploaded_by = None
            project.zip_original_name = ""
            project.zip_size = 0
            project.zip_uploaded_at = None
            project.save(update_fields=[
                "completed_zip", "zip_uploaded_by", "zip_original_name", "zip_size", "zip_uploaded_at",
            ])
    except Exception:
        pass


class TaskViewSet(viewsets.ModelViewSet):
    """Full CRUD for TasksPage.jsx (list/create/retrieve/update/delete),
    plus the handful of actions the page needs beyond plain field edits —
    completing a task, adding an attachment at any time, and ticking a
    subtask — each mirroring the frontend function of the same purpose
    so the page's own logic doesn't have to change, only where it reads
    from / writes to (this API instead of localStorage)."""

    queryset = Task.objects.select_related("client").all()
    serializer_class = TaskSerializer
    # Server-side enforcement of the Page Access / Module Access tables for
    # the "Tasks" page (see users/access.py): the caller must be allowed to
    # open the Tasks page AND hold the matching view/create/edit/delete
    # flag. Client-role tokens are always refused.
    module_name = "Tasks"
    permission_classes = [permissions.IsAuthenticated, ModuleAccess]

    def get_queryset(self):
        qs = super().get_queryset()
        params = self.request.query_params

        status_param = params.get("status")
        if status_param:
            qs = qs.filter(status=status_param)

        project = params.get("project")
        if project:
            qs = qs.filter(project=project)

        client_id = params.get("clientId")
        if client_id:
            qs = qs.filter(client_id=client_id)

        assignee = params.get("assignee")
        if assignee:
            # assignees is a JSON list of names — contains() matches
            # tasks where that exact name is one of the (up to 3) assignees.
            qs = qs.filter(assignees__contains=[assignee])

        return qs

    def perform_create(self, serializer):
        # createdBy falls back to the logged-in user's name, same as
        # `currentUserName || "Admin"` on the frontend's CreateTaskModal
        # onCreate handler.
        created_by = serializer.validated_data.get("created_by") or getattr(
            self.request.user, "name", ""
        ) or "Admin"
        serializer.save(created_by=created_by)

    # FIX (Priority 2 — "ticked module on Task Page, Client Page never
    # showed it ticked"): nothing anywhere in this app ever wrote to
    # projects.Module.status. The `complete` action below only ever
    # touched the Task row itself; ClientsPage/ClientPortal both read
    # completion off the linked Module (see
    # dashboard/serializers.py's module counts + status field), which
    # stayed "Pending"/"In Progress" forever regardless of what
    # happened on the Task Page. This keeps Module.status in lockstep
    # with its Task any time the Task's status changes — whether that
    # happens through the `complete` action or a plain PATCH/PUT from
    # TasksPage.jsx (see perform_update below), so it doesn't matter
    # which path the frontend actually uses to mark a task done.
    def _sync_linked_module_status(self, task):
        if not task.module_id:
            return
        if task.status == "Completed":
            new_status = ModuleStatusChoices.COMPLETED
        elif task.progress and task.progress > 0:
            new_status = ModuleStatusChoices.IN_PROGRESS
        else:
            new_status = ModuleStatusChoices.PENDING
        Module.objects.filter(pk=task.module_id).exclude(status=new_status).update(status=new_status)

    def perform_update(self, serializer):
        task = serializer.save()
        self._sync_linked_module_status(task)

    @action(detail=False, methods=["post"], url_path="bulk-create")
    def bulk_create(self, request):
        """POST /api/tasks/tasks/bulk-create/  body: {"tasks": [ {...}, {...} ]}
        Used for role-template tasks — one task per module, same
        assignees/sampleFiles on each (see the `createdList` map in
        TasksPage.jsx's CreateTaskModal onCreate). Plain single-task
        creates should keep using the normal POST /tasks/ endpoint."""

        items = request.data.get("tasks")
        if not isinstance(items, list) or not items:
            return Response({"detail": "\"tasks\" must be a non-empty list."}, status=400)

        created_by = getattr(request.user, "name", "") or "Admin"
        serializers_list = []
        for item in items:
            ser = TaskBulkCreateItemSerializer(data=item)
            ser.is_valid(raise_exception=True)
            serializers_list.append(ser)

        tasks = [ser.save(created_by=created_by) for ser in serializers_list]
        return Response(TaskSerializer(tasks, many=True).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def complete(self, request, pk=None):
        """POST /api/tasks/tasks/{id}/complete/  body: {"link": "...", "attachments": [...]}
        Mirrors markCompleted(id, link, newAttachments) — refuses locked
        module tasks exactly like the frontend does, marks every subtask
        done, sets progress to 100, and appends any new attachments
        instead of overwriting the existing list."""

        task = self.get_object()
        if task.locked:
            return Response(
                {"detail": "This module is locked until the client's request to start it is accepted."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        body = TaskCompleteSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        link = body.validated_data.get("link", "")
        new_attachments = body.validated_data.get("attachments", [])

        task.status = "Completed"
        task.progress = 100
        task.subtasks = [{**s, "done": True} for s in (task.subtasks or [])]
        if link:
            task.attachment = link
        if new_attachments:
            task.attachments = [*(task.attachments or []), *new_attachments]
        task.save()
        self._sync_linked_module_status(task)

        return Response(TaskSerializer(task).data)

    @action(detail=True, methods=["post"], url_path="add-attachment")
    def add_attachment(self, request, pk=None):
        """POST /api/tasks/tasks/{id}/add-attachment/  body: {"attachment": {...}}
        Mirrors addTaskAttachment(id, attachment) — lets a
        screenshot/video/link be attached any time, not only at
        completion."""

        task = self.get_object()
        body = TaskAddAttachmentSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        task.attachments = [*(task.attachments or []), body.validated_data["attachment"]]
        task.save(update_fields=["attachments", "updated_at"])
        return Response(TaskSerializer(task).data)

    @action(detail=True, methods=["post"], url_path="remove-attachment")
    def remove_attachment(self, request, pk=None):
        """POST /api/tasks/tasks/{id}/remove-attachment/
        body: {"attachmentId": <id>, "attachment": {type, name, url, zipFileId, localOnly}}

        Deletes ONE attachment everywhere it lives, so that removing it on the
        Tasks page also removes it from the Clients page / Client Portal:
          * zip uploaded through /zip/  -> TaskZipFile row + its file on disk
            + the ModuleFile mirror upload_zip made for the Clients page
          * file with a real ModuleFile pk (upload-file-attachment) -> that
            ModuleFile row + its file on disk
          * link -> the linked Module.url is cleared (or moved to the next
            remaining link) when it was this link (skipped for localOnly)
          * any other legacy file (att-... id, uploaded through the module
            sync) -> the newest ModuleFile of the task's module with the same
            file name, unless attachment.localOnly is sent
        Which module: the task's own Module FK — but older tasks were saved
        before that FK was stamped (Task.module is NULL) even though their
        files/links DID reach the Clients page. For those the frontend sends
        "moduleId" (the module's real pk, read from its Clients cache); it is
        only trusted when that module belongs to the SAME client as the task,
        and the task's FK is repaired from it so this never happens twice.
        The JSON entry in task.attachments is always removed. The optional
        "attachment" object only tells us what the entry was when it is not
        (or no longer) in task.attachments — it never widens what may be
        deleted: everything is still looked up inside THIS task / its module.
        Response = the task, plus "deletedModuleFileIds" so the frontend can
        drop the matching copies from its own Clients-page cache."""

        task = self.get_object()
        att_id = request.data.get("attachmentId")
        hint = request.data.get("attachment")
        hint = hint if isinstance(hint, dict) else {}
        if att_id is None and hint.get("zipFileId") in (None, ""):
            return Response({"error": "attachmentId is required."}, status=400)

        att_id_str = "" if att_id is None else str(att_id)

        # The module whose files/link must be cleaned (see docstring).
        module = task.module if task.module_id else None
        if module is None:
            hint_module_id = request.data.get("moduleId")
            if str(hint_module_id).isdigit() and task.client_id:
                candidate = Module.objects.select_related("project").filter(pk=int(hint_module_id)).first()
                if candidate and candidate.project.client_id == task.client_id:
                    module = candidate
        module_id = module.id if module else None

        def _is_target(a):
            return isinstance(a, dict) and att_id_str != "" and str(a.get("id", "")) == att_id_str

        entries = [a for a in (task.attachments or []) if _is_target(a)]
        info = entries[0] if entries else {}
        a_type = info.get("type") or hint.get("type") or ""
        a_name = info.get("name") or hint.get("name") or ""
        a_url = info.get("url") or hint.get("url") or ""
        zip_ref = info.get("zipFileId") if info.get("zipFileId") not in (None, "") else hint.get("zipFileId")

        deleted_module_file_ids = []

        # 1) Zip uploaded through /zip/ (frontend keeps its id as "task-<pk>").
        zip_pk = None
        if zip_ref not in (None, ""):
            digits = str(zip_ref).replace("task-", "", 1)
            zip_pk = int(digits) if digits.isdigit() else None
        if zip_pk is not None:
            zip_file = TaskZipFile.objects.filter(pk=zip_pk, task=task).first()
            if zip_file:
                deleted_module_file_ids += _purge_task_zip(task, zip_file, module_id)
            else:
                # Row already gone — still drop the leftover JSON entries.
                _drop_zip_entries(task, zip_pk)

        # 2) A real ModuleFile pk (upload-file-attachment).
        elif att_id_str.isdigit():
            mf = ModuleFile.objects.filter(pk=int(att_id_str)).first()
            if mf:
                if not module_id or mf.module_id != module_id:
                    return Response({"error": "That file does not belong to this task's module."}, status=400)
                deleted_module_file_ids.append(mf.id)
                mf.file.delete(save=False)
                mf.delete()
            # already gone -> still remove the JSON entry below

        # 3) A link: it lives on Module.url (one per module).
        elif a_type == "link":
            # (localOnly = a link on a sub-task row, which was never synced to
            # Module.url, so there is nothing to clear there.)
            if module and a_url and not hint.get("localOnly"):
                if (module.url or "").strip().rstrip("/") == str(a_url).strip().rstrip("/"):
                    remaining = [
                        x for x in (task.attachments or [])
                        if isinstance(x, dict) and not _is_target(x) and x.get("type") == "link" and x.get("url")
                    ]
                    # Module.url is one link, last one added wins — fall back
                    # to the newest link still attached to the task, if any.
                    module.url = str(remaining[-1]["url"]) if remaining else ""
                    module.url_approved = False
                    module.url_approved_by = None
                    module.url_approved_at = None
                    module.save(update_fields=["url", "url_approved", "url_approved_by", "url_approved_at", "updated_at"])

        # 4) Legacy file (att-... id): the module sync uploaded it as its own
        #    ModuleFile without telling the task, so find it by name.
        elif a_type != "link" and a_name and module_id and not hint.get("localOnly"):
            mf = (
                ModuleFile.objects.filter(module_id=module_id, original_name=a_name)
                .order_by("-id")
                .first()
            )
            if mf:
                deleted_module_file_ids.append(mf.id)
                mf.file.delete(save=False)
                mf.delete()

        # Remove the matching JSON entry/entries from task.attachments (the
        # frontend's own add-attachment call and the upload endpoints can each
        # leave one, so there can be two with the same id).
        task.refresh_from_db(fields=["attachments", "attachment"])
        before = len(task.attachments or [])
        task.attachments = [a for a in (task.attachments or []) if not _is_target(a)]
        if len(task.attachments) == before and att_id_str.isdigit():
            # Also try matching by the legacy "zipFileId" key so zip entries
            # stored before the id-normalisation are still removable.
            task.attachments = [
                a for a in (task.attachments or [])
                if not (isinstance(a, dict) and str(a.get("zipFileId", "")) == att_id_str)
            ]

        # Also clear task.attachment (the single primary link field) if it
        # matches the removed id — keeps the legacy single-link field tidy.
        fields = ["attachments", "updated_at"]
        if task.attachment and att_id_str and str(task.attachment) == att_id_str:
            task.attachment = ""
            fields.append("attachment")
        if module and not task.module_id:
            task.module = module  # repair the missing FK so later uploads/deletes find the module
            fields.append("module")
        task.save(update_fields=fields)

        data = dict(TaskSerializer(task, context={"request": request}).data)
        data["deletedModuleFileIds"] = deleted_module_file_ids
        data["moduleResolved"] = module is not None
        return Response(data)

    @action(detail=True, methods=["post"], url_path="toggle-subtask")
    def toggle_subtask(self, request, pk=None):
        """POST /api/tasks/tasks/{id}/toggle-subtask/  body: {"subtaskId": 3}
        Mirrors toggleSubtask(taskId, subId) — flips one subtask's
        `done`, then recomputes progress the same way: percentage of
        subtasks done, unless the task itself is already Completed (100
        either way)."""

        task = self.get_object()
        body = TaskToggleSubtaskSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        sub_id = body.validated_data["subtaskId"]

        subtasks = [
            {**s, "done": not s["done"]} if s.get("id") == sub_id else s
            for s in (task.subtasks or [])
        ]
        task.subtasks = subtasks
        if task.status == "Completed":
            task.progress = 100
        elif subtasks:
            done_count = sum(1 for s in subtasks if s.get("done"))
            task.progress = round((done_count / len(subtasks)) * 100)
        task.save(update_fields=["subtasks", "progress", "updated_at"])
        self._sync_linked_module_status(task)
        return Response(TaskSerializer(task).data)

    # -- Zip attachments (real backend storage, multiple per task) ------
    # Wired manually in tasks/urls.py (not via the DefaultRouter) so the
    # <file_id> segment can sit in the URL — same manual-path style
    # projects/urls.py already uses for its nested module files.

    def zip_files(self, request, pk=None):
        """GET /api/tasks/tasks/<id>/zip/ — list this task's zip files."""
        task = self.get_object()
        files = task.zip_files.all()
        return Response(TaskZipFileSerializer(files, many=True, context={"request": request}).data)

    def upload_zip(self, request, pk=None):
        """POST /api/tasks/tasks/<id>/zip/  (multipart, field name "zip")
        Same idea as ProjectViewSet.upload_zip: asal bytes disk par jaate
        hain, Postgres mein sirf path + metadata. Task ke apne
        `attachments` JSON list mein bhi ek chhoti si entry daal dete
        hain (type: "zip") taake task ka apna attachment list bhi ise
        dikhata rahe — sirf ab base64 ki jagah real file ID hai."""
        task = self.get_object()
        file_obj = request.FILES.get("zip")
        if not file_obj:
            return Response({"error": "No zip uploaded."}, status=400)
        if not file_obj.name.lower().endswith(".zip"):
            return Response({"error": "Only .zip files are allowed."}, status=400)

        zip_file = TaskZipFile.objects.create(
            task=task,
            file=file_obj,
            original_name=file_obj.name,
            size=file_obj.size,
            uploaded_by=request.user,
        )
        task.attachments = [
            *(task.attachments or []),
            {"type": "zip", "name": file_obj.name, "zipFileId": zip_file.id, "addedAt": timezone.now().isoformat()},
        ]
        task.save(update_fields=["attachments", "updated_at"])

        # FIX: also mirror this ZIP into the linked Module's ModuleFile
        # table so ClientsPage and ClientPortal see it without needing
        # any browser's localStorage. Best-effort — the ZIP is saved
        # either way, the mirror just adds cross-browser visibility.
        if task.module_id:
            try:
                zip_file.file.seek(0)  # rewind after TaskZipFile.save()
                ModuleFile.objects.create(
                    module_id=task.module_id,
                    file=zip_file.file,
                    original_name=file_obj.name,
                    mime_type="application/zip",
                    size=file_obj.size,
                    uploaded_by=request.user,
                )
            except Exception:
                pass  # cross-browser mirror is best-effort

        # Final project deliverable (Tasks page sends final=1) -> also the
        # project's own completed_zip, which Clients page / Portal read.
        if str(request.data.get("final", "")).lower() in ("1", "true", "yes"):
            _mirror_final_zip_to_project(task, zip_file, request.user)

        return Response(TaskZipFileSerializer(zip_file, context={"request": request}).data, status=201)

    def download_zip(self, request, pk=None, file_id=None):
        """GET /api/tasks/tasks/<id>/zip/<file_id>/download/"""
        zip_file = get_object_or_404(TaskZipFile, pk=file_id, task_id=pk)
        return FileResponse(zip_file.file.open("rb"), as_attachment=True, filename=zip_file.original_name)

    def delete_zip(self, request, pk=None, file_id=None):
        """DELETE /api/tasks/tasks/<id>/zip/<file_id>/
        Removes the TaskZipFile AND everything upload_zip mirrored from it
        (the ModuleFile shown on the Clients page / Client Portal and the
        entry in task.attachments) — before, only the TaskZipFile went, so
        the zip kept showing on the Clients page."""
        zip_file = get_object_or_404(TaskZipFile, pk=file_id, task_id=pk)
        file_name = zip_file.original_name
        deleted_ids = _purge_task_zip(zip_file.task, zip_file)
        return Response({"message": f"{file_name} deleted.", "deletedModuleFileIds": deleted_ids})

    @action(detail=True, methods=["post"], url_path="upload-file-attachment")
    def upload_file_attachment(self, request, pk=None):
        """POST /api/tasks/tasks/{id}/upload-file-attachment/  (multipart)
        Accepts a raw file (field name \"file\") and:
          1. Creates a ModuleFile on the task's linked Module (if any) so
             ClientsPage and ClientPortal can see it cross-browser via the
             /api/dashboard/clients/ serializer (_module_attachments).
          2. Appends a JSON metadata entry to task.attachments with a real
             HTTP URL so the task's own attachment list shows it.
        For ZIP files, prefer the dedicated upload_zip endpoint — this one
        handles PDFs, images, videos, and other non-ZIP file types."""
        task = self.get_object()
        file_obj = request.FILES.get("file")
        if not file_obj:
            return Response({"error": "No file uploaded."}, status=400)

        if not task.module_id:
            return Response(
                {"error": "This task has no linked backend Module. Upload cannot be mirrored to the Client Portal."},
                status=400,
            )

        module_file = ModuleFile.objects.create(
            module_id=task.module_id,
            file=file_obj,
            original_name=file_obj.name,
            mime_type=file_obj.content_type or "",
            size=file_obj.size,
            uploaded_by=request.user,
        )

        # Build an absolute URL the browser can immediately open/download.
        file_url = request.build_absolute_uri(module_file.file.url)

        mime = (file_obj.content_type or "").lower()
        name_lower = file_obj.name.lower()
        if mime.startswith("image/"):
            ftype = "image"
        elif mime.startswith("video/"):
            ftype = "video"
        elif mime in ("application/zip", "application/x-zip-compressed") or name_lower.endswith(".zip"):
            ftype = "zip"
        else:
            ftype = "file"

        attachment_entry = {
            "id": module_file.id,
            "type": ftype,
            "name": file_obj.name,
            "url": file_url,
            "addedAt": timezone.now().isoformat(),
        }
        task.attachments = [*(task.attachments or []), attachment_entry]
        task.save(update_fields=["attachments", "updated_at"])

        from .serializers import TaskSerializer
        return Response(TaskSerializer(task, context={"request": request}).data, status=201)