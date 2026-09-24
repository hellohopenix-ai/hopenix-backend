from django.conf import settings as dj_settings
from rest_framework import serializers
from .models import (
    Client, Order, Expense, Income, Invoice, ModuleRequest, IntakeRequest,
    ActivityLogEntry, ClientMessage, Document,
)


def _module_attachments(obj, request):
    """Shared by ClientProjectModuleSerializer and its sub-module
    counterpart below — builds the exact `attachments` shape
    ClientPortal.jsx's GatedModuleAttachments/AttachmentGallery already
    render (type: "link" | "image" | "video" | "zip" | "file", name,
    url), sourced straight off the real projects.Module row (its `url`
    field for the live/staging link, its `files` relation for anything
    the team actually uploaded — see projects.ModuleFile).

    FIX (Client Portal attachments were always empty): this data existed
    in Postgres the whole time (ClientsPage.jsx's admin side has written
    real Module.url / ModuleFile rows since the sub-task backend fix),
    but this serializer never exposed it here, so the client-facing
    /api/dashboard/clients/ response stopped at id/name/status/unlocked
    and GatedModuleAttachments always got an empty array — the module
    itself showed as done, its proof never did.
    """
    items = []

    user = getattr(request, "user", None)
    is_client_viewer = bool(user) and getattr(user, "role", None) == "client"

    # FIX (link showed on the Client Portal before any admin approval): only
    # files were gated by approval; the live/staging link was always sent to
    # the client the moment a Task Page user pasted it. It now needs the same
    # "Approved / Show on Portal" tick (Module.url_approved). Staff still get
    # the link either way, with its approved flag, so they have something to
    # review.
    if obj.url and (not is_client_viewer or obj.url_approved):
        items.append({
            "id": f"link-{obj.id}",
            "type": "link",
            "name": obj.url,
            "url": obj.url,
            "approved": obj.url_approved,
        })

    # FIX (Section 6/7 — a Task Page upload must not immediately become
    # visible on the Client Portal): a client viewing this must only ever
    # see files staff has actually ticked "Approved / Show on Portal"
    # (ModuleFile.approved). Staff/admin viewing the exact same endpoint
    # (ClientsPage.jsx) still needs to see EVERY file, approved or still
    # pending_review, so they have something to review in the first
    # place — so the filter only applies when the request is a client.
    files = obj.files.filter(approved=True) if is_client_viewer else obj.files.all()

    for f in files:
        file_url = None
        if f.file:
            file_url = request.build_absolute_uri(f.file.url) if request else f.file.url

        mime = (f.mime_type or "").lower()
        name_lower = (f.original_name or "").lower()
        if mime.startswith("image/"):
            ftype = "image"
        elif mime.startswith("video/"):
            ftype = "video"
        elif mime in ("application/zip", "application/x-zip-compressed") or name_lower.endswith(".zip"):
            ftype = "zip"
        else:
            ftype = "file"

        items.append({
            "id": f.id,
            "type": ftype,
            "name": f.original_name,
            "url": file_url,
            "size": f.size,
            "uploadedAt": f.uploaded_at,
            # Staff-only in practice (clients never receive unapproved rows
            # to begin with) — lets ClientsPage.jsx render the
            # "Approved / Show on Portal" checkbox's current state.
            "approved": f.approved,
        })

    return items


class ClientProjectSubModuleSerializer(serializers.Serializer):
    """One sub-task under a module (e.g. Development's Frontend/Backend/
    ...) — same shape as ClientProjectModuleSerializer below, one level
    deep only. Sourced off the real Module.subtasks relation now (see
    projects.Module.parent) instead of never reaching the Client Portal
    at all."""

    id = serializers.IntegerField()
    name = serializers.CharField()
    status = serializers.CharField()
    unlocked = serializers.BooleanField()
    attachments = serializers.SerializerMethodField()

    def get_attachments(self, obj):
        return _module_attachments(obj, self.context.get("request"))


class ClientProjectModuleSerializer(serializers.Serializer):
    """One row in a project's module list — just enough for
    ClientPortal.jsx to render each module's lock state and let the
    client pick one to request. Sourced straight off projects.Module,
    which is why this stays a plain Serializer (not ModelSerializer)
    exactly like ClientProjectSummarySerializer below it — no import of
    projects.models needed here."""

    id = serializers.IntegerField()
    name = serializers.CharField()
    status = serializers.CharField()
    unlocked = serializers.BooleanField()
    subModules = ClientProjectSubModuleSerializer(source="subtasks", many=True, read_only=True)
    attachments = serializers.SerializerMethodField()

    def get_attachments(self, obj):
        return _module_attachments(obj, self.context.get("request"))


class ClientProjectSummarySerializer(serializers.Serializer):
    """Light-weight nested "projects" list on a Client — just enough for
    ClientsPage.jsx's client card/detail view (name, type, status,
    budget, progress) AND ClientPortal.jsx's own project cards, which
    also need each module's name/status/lock-state to render the
    module list and let a client pick one to request. The FULL project
    (files, brief, ...) is still fetched from /api/projects/<id>/ when
    the admin opens it — only modules are duplicated here, since the
    client portal never hits the projects app directly."""

    id = serializers.IntegerField()
    name = serializers.CharField()
    project_type = serializers.CharField()
    status = serializers.CharField()
    budget = serializers.DecimalField(max_digits=12, decimal_places=2)
    spent = serializers.DecimalField(max_digits=12, decimal_places=2)
    # source="top_level_modules...": sub-tasks nest inside their own
    # module's `subModules` instead of also being counted/listed here a
    # second time as a flat duplicate entry (see projects.Project).
    module_count = serializers.IntegerField(source="top_level_modules.count")
    modules_done = serializers.SerializerMethodField()
    modules = ClientProjectModuleSerializer(source="top_level_modules", many=True, read_only=True)
    # FIX (Final Deliverable block in ClientPortal.jsx always rendered as
    # nothing at all, not even the locked card): this serializer never
    # exposed the project's own completed_zip — ClientPortal.jsx reads
    # `p.deliverableZip` and only renders <FinalDeliverableBlock> when
    # that's truthy, so it was permanently undefined regardless of
    # whether the admin had uploaded a zip or the client had paid.
    deliverableZip = serializers.SerializerMethodField()

    def get_modules_done(self, obj):
        return obj.top_level_modules.filter(status="Completed").count()

    def get_deliverableZip(self, obj):
        if not obj.completed_zip:
            return None
        request = self.context.get("request")
        url = request.build_absolute_uri(obj.completed_zip.url) if request else obj.completed_zip.url
        # size/uploadedAt added so the Clients page card can show them
        # after a refresh (it displays `zip.size` as-is).
        size_bytes = obj.zip_size or 0
        if size_bytes >= 1024 * 1024:
            size_label = f"{size_bytes / (1024 * 1024):.1f} MB"
        elif size_bytes:
            size_label = f"{max(1, round(size_bytes / 1024))} KB"
        else:
            size_label = ""
        return {
            "id": f"project-zip-{obj.id}",
            "name": obj.zip_original_name or obj.completed_zip.name.split("/")[-1],
            "url": url,
            "size": size_label,
            "uploadedAt": obj.zip_uploaded_at.isoformat() if obj.zip_uploaded_at else None,
        }


class DocumentSerializer(serializers.ModelSerializer):
    """One uploaded document — staff creates via POST (multipart), clients
    and staff read via GET, anyone with permission deletes via DELETE.
    `file_url` is the absolute URL the browser can fetch/download
    directly (honoring MEDIA_URL / the dev staticfiles trick in urls.py)."""

    file_url = serializers.SerializerMethodField()
    client_name = serializers.CharField(source="client.name", read_only=True)
    project_name = serializers.CharField(source="project.name", read_only=True, default="")
    uploaded_by_name = serializers.SerializerMethodField()

    class Meta:
        model = Document
        fields = [
            "id",
            "file",
            "file_url",
            "file_name",
            "file_size",
            "uploaded_at",
            "client",
            "client_name",
            "project",
            "project_name",
            "uploaded_by",
            "uploaded_by_name",
        ]
        read_only_fields = ["id", "uploaded_at", "uploaded_by"]
        # `client` must be optional so portal clients can POST without
        # sending a client id — perform_create forces it from the token
        # user's client_profile, so supplying it from the browser would
        # be redundant AND a security hole (anyone could spoof another
        # client's id). file_name / file_size are also optional: they
        # have safe defaults and perform_create fills them in.
        extra_kwargs = {
            "client": {"required": False},
            "file_name": {"required": False},
            "file_size": {"required": False},
        }

    def get_file_url(self, obj):
        if not obj.file:
            return None
        request = self.context.get("request")
        if request is not None:
            return request.build_absolute_uri(obj.file.url)
        # Fallback: build manually from MEDIA_URL
        return f"{dj_settings.MEDIA_URL}{obj.file.name}"

    def get_uploaded_by_name(self, obj):
        if obj.uploaded_by_id is None:
            return ""
        return getattr(obj.uploaded_by, "name", "") or getattr(obj.uploaded_by, "username", "")


class ClientSerializer(serializers.ModelSerializer):
    total_orders_count = serializers.ReadOnlyField()
    total_spent = serializers.ReadOnlyField()
    manager_name = serializers.CharField(source="manager.name", read_only=True, default="")
    portal_email = serializers.CharField(source="portal_user.email", read_only=True, default="")
    has_portal_access = serializers.SerializerMethodField()
    documents = DocumentSerializer(many=True, read_only=True)
    projects = ClientProjectSummarySerializer(many=True, read_only=True)
    outstanding = serializers.SerializerMethodField()
    # camelCase alias — ClientBirthdayCelebration.jsx reads
    # client.dateOfBirth || client.date_of_birth || client.dob.
    dateOfBirth = serializers.SerializerMethodField()

    class Meta:
        model = Client
        fields = [
            "id", "name", "email", "industry", "contact_person", "phone",
            "address", "notes", "country", "country_code", "city",
            "date_of_birth", "dateOfBirth",
            "avatar", "avatar_url", "status", "manager", "manager_name",
            "has_portal_access", "portal_email", "created_at",
            "total_orders_count", "total_spent", "outstanding", "projects",
            "documents",
        ]
        read_only_fields = ["id", "created_at"]

    def get_has_portal_access(self, obj):
        return obj.portal_user_id is not None

    def get_dateOfBirth(self, obj):
        return obj.date_of_birth.isoformat() if obj.date_of_birth else ""

    def get_outstanding(self, obj):
        # Sum of every project budget attached to this client, minus what
        # has actually been paid — same "Total Client Revenue" math
        # ClientsPage.jsx's counts useMemo used to do client-side.
        total_budget = sum((p.budget for p in obj.projects.all()), start=0)
        return total_budget - (obj.total_spent or 0)

    def validate_email(self, value):
        if value:
            qs = Client.objects.filter(email__iexact=value)
            if self.instance:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise serializers.ValidationError("A client with this email already exists.")
        return value


class OrderSerializer(serializers.ModelSerializer):
    client_name = serializers.CharField(source="client.name", read_only=True)

    class Meta:
        model = Order
        fields = ["id", "client", "client_name", "amount", "status", "note", "created_at"]


class ExpenseSerializer(serializers.ModelSerializer):
    class Meta:
        model = Expense
        fields = ["id", "title", "category", "amount", "date"]


class IncomeSerializer(serializers.ModelSerializer):
    """IncomePage.jsx ke fields se seedha match — "desc" hi ek jagah hai
    jahan naam alag hai (model field "description" hai), baaki sab keys
    frontend jaisi ki taisi. received_on model.save() khud set karta hai
    (status Received ho tabhi), isliye yahan sirf read-only hai — client
    se seedha likha nahi ja sakta.
    """

    desc = serializers.CharField(source="description", max_length=255)
    created_by_name = serializers.CharField(source="created_by.name", read_only=True, default="")

    class Meta:
        model = Income
        fields = [
            "id", "date", "desc", "project", "client", "amount",
            "method", "status", "received_on", "created_by_name",
            "created_at", "updated_at",
        ]
        read_only_fields = ["id", "received_on", "created_by_name", "created_at", "updated_at"]

    def validate_amount(self, value):
        if value is None or value <= 0:
            raise serializers.ValidationError("Amount must be greater than 0.")
        return value


class InvoiceSerializer(serializers.ModelSerializer):
    client_name = serializers.CharField(source="client.name", read_only=True)
    project_name = serializers.CharField(source="project.name", read_only=True, default="")

    class Meta:
        model = Invoice
        fields = [
            "id", "client", "client_name", "project", "project_name",
            "milestone_number", "milestone_total", "percent", "amount", "paid_amount",
            "status", "payment_proof", "note", "number", "issue_date", "due_date",
            "bill_to", "line_items", "po_number", "payment_method", "transaction_ref",
            "amount_received", "discount", "subtotal", "grand_total",
            "approved_by", "designation", "approval_date",
            "submitted_at", "paid_at", "created_at",
        ]
        # `number` is deliberately writable — ClientsPage.jsx generates the
        # invoice number client-side and sends it here so the admin's
        # local ledger and the real backend row (what the Client Portal
        # reads) always agree; the model's save() only auto-generates one
        # as a fallback when a row is created without it.
        read_only_fields = ["status", "paid_amount", "submitted_at", "paid_at", "created_at"]


class ModuleRequestSerializer(serializers.ModelSerializer):
    client_name = serializers.CharField(source="client.name", read_only=True)
    module_name = serializers.ReadOnlyField()
    project_name = serializers.ReadOnlyField()

    class Meta:
        model = ModuleRequest
        fields = [
            "id", "client", "client_name", "module", "module_name",
            "project", "project_name", "custom_module_name", "note",
            "attachment", "attachment_link",
            "status", "requested_at", "decided_at", "decided_by",
        ]
        read_only_fields = ["status", "requested_at", "decided_at", "decided_by"]

    def validate(self, data):
        module = data.get("module")
        project = data.get("project")
        custom_name = (data.get("custom_module_name") or "").strip()
        if not module and not (project and custom_name):
            raise serializers.ValidationError(
                "Either pick an existing module, or give a project and a name for the new module you're requesting."
            )
        if module and (project or custom_name):
            raise serializers.ValidationError("Pick an existing module OR describe a new one — not both.")
        return data


class IntakeRequestSerializer(serializers.ModelSerializer):
    class Meta:
        model = IntakeRequest
        fields = [
            "id", "company_name", "contact_person", "email", "phone", "city",
            "project_name", "project_type", "budget", "requirements",
            "payment_proof", "status", "resulting_client", "created_at",
        ]
        read_only_fields = ["status", "resulting_client", "created_at"]

class ActivityLogEntrySerializer(serializers.ModelSerializer):
    class Meta:
        model = ActivityLogEntry
        fields = ["id", "client", "text", "created_at"]
        read_only_fields = ["id", "created_at"]


class ClientMessageSerializer(serializers.ModelSerializer):
    class Meta:
        model = ClientMessage
        fields = ["id", "client", "sender", "kind", "subject", "text", "attachment", "created_at"]
        read_only_fields = ["id", "sender", "created_at"]