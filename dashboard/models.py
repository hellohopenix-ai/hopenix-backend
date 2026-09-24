import uuid

from django.conf import settings
from django.db import models


def invoice_proof_path(instance, filename):
    """media/invoices/<client_id>/<uuid>_<filename> — the payment-proof
    screenshot a client uploads against a milestone invoice (ClientsPage.jsx
    Billing tab / Client Portal). Real bytes on disk, only the relative
    path in Postgres — same pattern as projects.module_file_path."""
    return f"invoices/{instance.client_id}/{uuid.uuid4()}_{filename}"


def intake_proof_path(instance, filename):
    """media/intake/<uuid>_<filename> — the advance-payment screenshot
    attached to a public ClientIntakeForm.jsx submission, before any
    Client row (and therefore any client id) exists yet."""
    return f"intake/{uuid.uuid4()}_{filename}"


def client_avatar_path(instance, filename):
    """media/clients/<client_id-or-new>/avatar/<uuid>_<filename> — the
    real uploaded file behind ClientsPage.jsx's Add/Edit Client photo
    picker. "new" covers the brief window on create, before the row
    (and therefore instance.pk) exists yet."""
    return f"clients/{instance.pk or 'new'}/avatar/{uuid.uuid4()}_{filename}"


def module_request_attachment_path(instance, filename):
    return f"clients/{instance.client_id}/module_requests/{uuid.uuid4()}_{filename}"


def client_message_attachment_path(instance, filename):
    return f"clients/{instance.client_id}/messages/{uuid.uuid4()}_{filename}"


def document_file_path(instance, filename):
    """media/documents/<client_id>/<uuid>_<filename> — a real uploaded
    client document (contract, brief, NDA, etc.) persisted on disk,
    only the relative path stored in Postgres — same pattern as the
    other upload helpers above."""
    return f"documents/{instance.client_id}/{uuid.uuid4()}_{filename}"


class Client(models.Model):
    """ClientsPage.jsx's "company" record. Everything about a company that
    is describable on its own (name, contact info, who manages it, its
    portal login) lives here. A client's projects/modules are real rows on
    projects.Project / projects.Module (FK'd to this Client, see
    projects.Project.client) — not duplicated on this model — and a
    client's tasks are real rows on tasks.Task (also FK'd here)."""

    STATUS_CHOICES = [
        ("active", "Active"),
        ("inactive", "Inactive"),
    ]

    name = models.CharField(max_length=150)
    email = models.EmailField(blank=True, null=True)

    # NEW: these existed only implicitly in ClientsPage.jsx's local
    # "company" object before — added so a client created here carries
    # everything the Add/Edit Client form and the client card/detail
    # view actually show.
    industry = models.CharField(max_length=100, blank=True)
    contact_person = models.CharField(max_length=150, blank=True)
    phone = models.CharField(max_length=30, blank=True)
    address = models.CharField(max_length=255, blank=True)
    notes = models.TextField(blank=True)

    country = models.CharField(max_length=100, blank=True)
    country_code = models.CharField(
        max_length=2,
        blank=True,
        help_text="2-letter ISO code, e.g. 'us', 'pk' — used for the flag icon on the dashboard.",
    )
    city = models.CharField(max_length=100, blank=True)

    # NEW — the client's date of birth (ClientsPage.jsx's Add/Edit Client
    # "Date of Birth" field). It only lived in the admin's own browser
    # before, so the Client Portal (a different browser/session) never
    # knew it and could not show the birthday celebration.
    date_of_birth = models.DateField(null=True, blank=True)

    # NEW — the real uploaded photo behind ClientsPage.jsx's Add/Edit
    # Client photo picker (sent as a real file in a multipart request,
    # see ClientsPage.jsx's buildClientPayload/dataUrlToFile). avatar_url
    # below stays as-is for anyone who just wants to paste a plain image
    # link instead — the serializer exposes both; the frontend prefers
    # a freshly-uploaded `avatar` over `avatar_url` once one exists.
    avatar = models.ImageField(upload_to=client_avatar_path, null=True, blank=True)
    avatar_url = models.URLField(blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="active")

    # Real manager/developer assignment, pulled from actual approved
    # users. Matches ClientsPage.jsx's `assignableTeam` exactly (see that
    # file's comment): any approved user who isn't an admin or a client
    # themselves — a developer can be assigned directly, not just a
    # "manager"-titled user — so this excludes admin/client rather than
    # only allowing role in ["manager", "admin"]. (That older, narrower
    # restriction is what caused "Invalid pk — object does not exist"
    # whenever a non-manager/admin team member was picked in the form:
    # DRF's auto-generated serializer field enforces limit_choices_to,
    # so a valid user id was still rejected as "doesn't exist".)
    manager = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="managed_clients",
        limit_choices_to=~models.Q(role__in=["admin", "client"]),
    )

    # NEW — the real, working login the client uses on ClientPortal.jsx —
    # a genuine users.User row with role="client", created/reset via
    # ClientViewSet.portal_access. Null until an admin generates access.
    portal_user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="client_profile",
    )

    # NEW
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="added_clients"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.name

    @property
    def total_orders_count(self):
        return self.orders.count()

    @property
    def total_spent(self):
        return self.orders.filter(status="approved").aggregate(
            total=models.Sum("amount")
        )["total"] or 0


class Order(models.Model):
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("approved", "Approved"),
        ("rejected", "Rejected"),
    ]

    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="orders")
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="approved")
    note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.client.name} — {self.amount}"


class Expense(models.Model):
    title = models.CharField(max_length=150)
    category = models.CharField(max_length=100, blank=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    date = models.DateField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date"]

    def __str__(self):
        return f"{self.title} — {self.amount}"


class Income(models.Model):
    """IncomePage.jsx ka real ledger — pehle ye sirf title/source/amount/date
    tha (frontend ke "Add Income" form se kabhi match nahi karta tha), ab
    IncomePage.jsx jo fields dikhata hai unhi ko 1:1 store karta hai, taake
    frontend aur Postgres row mein koi mapping-drift na rahe.

    method/status ke choices hi values hain (jaise projects.StatusChoices
    is app mein already karta hai) — koi alag "code -> label" tabdeeli
    nahi, isliye serializer bilkul seedha (no relabeling) rehta hai.
    """

    METHOD_CHOICES = [
        ("Bank Transfer", "Bank Transfer"),
        ("JazzCash", "JazzCash"),
        ("Easypaisa", "Easypaisa"),
        ("Cash in Hand", "Cash in Hand"),
    ]

    STATUS_CHOICES = [
        ("Received", "Received"),
        ("Pending", "Pending"),
        ("Overdue", "Overdue"),
    ]

    description = models.CharField(max_length=255)

    # Free-text jaise frontend ka datalist input hai — kisi strict
    # projects.Project ya dashboard.Client row se bandhay nahi, taake user
    # jo bhi naya project/client type kare wo bina extra setup ke turant
    # save ho jaye (IncomePage.jsx isi tarah kaam karta hai).
    project = models.CharField(max_length=150, blank=True, default="")
    client = models.CharField(max_length=150, blank=True, default="")

    amount = models.DecimalField(max_digits=12, decimal_places=2)
    method = models.CharField(max_length=20, choices=METHOD_CHOICES, default="Bank Transfer")
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="Received")

    date = models.DateField()
    # Sirf "Received" status par bharta hai — save() isi rule ko enforce
    # karta hai taake API se seedha bhi galat data na aa sake (frontend
    # jaisi hi logic, ab dono jaga se protected).
    received_on = models.DateField(null=True, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="income_entries"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-date", "-created_at"]
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["project"]),
        ]

    def __str__(self):
        return f"{self.description} — {self.amount}"

    def save(self, *args, **kwargs):
        self.received_on = self.date if self.status == "Received" else None
        super().save(*args, **kwargs)


class Invoice(models.Model):
    """One milestone of a client's project budget (ClientsPage.jsx Billing
    tab: budget split into N milestones, each with its own %/amount).
    A client submits a payment-proof screenshot from the Client Portal
    (status -> submitted); admin confirms it here (status -> paid)."""

    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("submitted", "Submitted"),  # client uploaded a payment proof, awaiting admin confirmation
        ("partial", "Partial"),  # admin recorded a payment that doesn't fully cover the amount yet
        ("paid", "Paid"),
    ]

    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="invoices")
    project = models.ForeignKey(
        "projects.Project", null=True, blank=True, on_delete=models.SET_NULL, related_name="invoices"
    )

    milestone_number = models.PositiveSmallIntegerField(default=1)
    milestone_total = models.PositiveSmallIntegerField(default=1)
    percent = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    paid_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="pending")
    payment_proof = models.FileField(upload_to=invoice_proof_path, null=True, blank=True)
    note = models.CharField(max_length=255, blank=True)

    # NEW — the full branded-document fields ClientsPage.jsx's
    # GenerateInvoiceModal / InvoiceDocumentPreview collect. These used to
    # live ONLY in the admin browser's local state (never reached
    # Postgres), so the real Client Portal — which reads straight from
    # this model, not from that browser's localStorage — could never
    # show a proper invoice. Kept as free-text/JSON (not strict Date
    # fields) since the frontend already sends these pre-formatted for
    # display, exactly as ClientsPage.jsx builds them locally.
    number = models.CharField(max_length=40, blank=True, default="")
    issue_date = models.CharField(max_length=40, blank=True, default="")
    due_date = models.CharField(max_length=40, blank=True, default="")
    bill_to = models.JSONField(default=dict, blank=True)
    line_items = models.JSONField(default=list, blank=True)
    po_number = models.CharField(max_length=100, blank=True, default="")
    payment_method = models.CharField(max_length=100, blank=True, default="")
    transaction_ref = models.CharField(max_length=150, blank=True, default="")
    amount_received = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    discount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    grand_total = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    approved_by = models.CharField(max_length=150, blank=True, default="")
    designation = models.CharField(max_length=150, blank=True, default="")
    approval_date = models.CharField(max_length=40, blank=True, default="")

    submitted_at = models.DateTimeField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["client_id", "milestone_number"]

    def __str__(self):
        return f"{self.client.name} — Milestone {self.milestone_number}/{self.milestone_total}"

    def save(self, *args, **kwargs):
        # Same INV-100x numbering ClientsPage.jsx already generates
        # client-side — only used as a fallback here (the frontend always
        # sends its own `number` now so admin/portal agree), so a row
        # created any other way (admin panel, shell) still gets one.
        is_new = self._state.adding
        super().save(*args, **kwargs)
        if is_new and not self.number:
            self.number = f"INV-{1000 + self.id}"
            super().save(update_fields=["number"])


class ModuleRequest(models.Model):
    """A client's "Request to start" click on a locked module
    (`module` set), OR a request for an entirely new module that isn't
    in the project's plan yet (`module` blank, `project` +
    `custom_module_name` set instead) — ClientPortal.jsx's
    ModuleRequestModal supports both. Staff accepting an EXISTING-module
    request just unlocks it (see ModuleRequestViewSet.accept); accepting
    a CUSTOM one creates the real projects.Module row first, then
    unlocks that."""

    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("accepted", "Accepted"),
        ("rejected", "Rejected"),
    ]

    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="module_requests")

    # Existing-module path: module is set, project is left blank (it's
    # implied by module.project).
    module = models.ForeignKey(
        "projects.Module", null=True, blank=True, on_delete=models.CASCADE, related_name="requests"
    )

    # Custom/new-module path: module is blank, these are used instead.
    project = models.ForeignKey(
        "projects.Project", null=True, blank=True, on_delete=models.CASCADE, related_name="custom_module_requests"
    )
    custom_module_name = models.CharField(max_length=200, blank=True, default="")
    note = models.TextField(blank=True, default="")
    attachment = models.FileField(upload_to=module_request_attachment_path, null=True, blank=True)
    attachment_link = models.URLField(blank=True, default="")

    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="pending")

    requested_at = models.DateTimeField(auto_now_add=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        ordering = ["-requested_at"]

    def __str__(self):
        name = self.module.name if self.module_id else (self.custom_module_name or "custom module")
        return f"{self.client.name} / {name} — {self.status}"

    @property
    def module_name(self):
        return self.module.name if self.module_id else self.custom_module_name

    @property
    def project_name(self):
        return self.module.project.name if self.module_id else (self.project.name if self.project_id else "")


class ActivityLogEntry(models.Model):
    """A flat, timestamped feed per client — module progress, payment
    updates, module-request decisions, etc. — shown on ClientPortal.jsx's
    Activity tab. Written automatically at the key transitions in
    views.py (submit_payment, confirm, module accept/reject) rather than
    exposed as a directly-writable endpoint."""

    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="activity_entries")
    text = models.CharField(max_length=500)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name_plural = "Activity log entries"

    def __str__(self):
        return self.text


class ClientMessage(models.Model):
    """One persisted thread per client — both the portal's Messages tab
    and its Support form write here, so a conversation survives logout/
    refresh and staff can see the same thread from the admin side."""

    SENDER_CHOICES = [
        ("client", "Client"),
        ("admin", "Admin"),
    ]

    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="messages")
    sender = models.CharField(max_length=10, choices=SENDER_CHOICES)
    kind = models.CharField(max_length=20, blank=True, default="")  # "", "support"
    subject = models.CharField(max_length=255, blank=True, default="")
    text = models.TextField()
    attachment = models.FileField(upload_to=client_message_attachment_path, null=True, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.get_sender_display()}: {self.text[:40]}"


class IntakeRequest(models.Model):
    """A public, no-login "New Project Request" submitted from
    ClientIntakeForm.jsx — anyone with the link can fill this in, no
    auth required (see IntakeRequestViewSet.get_permissions). Approving
    one (admin-only) creates a real Client + Project + Modules, exactly
    like a manual Add Client — see IntakeRequestViewSet.approve."""

    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("approved", "Approved"),
        ("rejected", "Rejected"),
    ]

    company_name = models.CharField(max_length=150, blank=True)
    contact_person = models.CharField(max_length=150, blank=True)
    email = models.EmailField()
    phone = models.CharField(max_length=30, blank=True)
    city = models.CharField(max_length=100, blank=True)

    project_name = models.CharField(max_length=200, blank=True)
    project_type = models.CharField(max_length=100, blank=True)
    budget = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    requirements = models.TextField(blank=True)
    payment_proof = models.FileField(upload_to=intake_proof_path, null=True, blank=True)

    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="pending")
    resulting_client = models.ForeignKey(Client, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.company_name or self.contact_person} — {self.status}"


class Document(models.Model):
    """A real uploaded file (PDF, DOCX, image, etc.) attached to a
    client — optionally also to one of their projects. Uploaded by any
    staff member through ClientsPage.jsx's Documents tab, or by the
    client themselves through the Client Portal's Documents tab.

    Bytes live in MEDIA_ROOT/documents/<client_id>/<uuid>_<filename>
    (see document_file_path above). Postgres stores only the relative
    path — same pattern as every other FileField in this app."""

    file = models.FileField(upload_to=document_file_path)
    file_name = models.CharField(max_length=255)
    # Human-readable size string (e.g. "1.2 MB") — computed and stored
    # at upload time so the list endpoint never needs to stat the file.
    file_size = models.CharField(max_length=50, blank=True, default="")
    uploaded_at = models.DateTimeField(auto_now_add=True)

    client = models.ForeignKey(
        Client,
        on_delete=models.CASCADE,
        related_name="documents",
    )
    # Optional: links the document to a specific project for this client.
    project = models.ForeignKey(
        "projects.Project",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="documents",
    )
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="uploaded_documents",
    )

    class Meta:
        ordering = ["-uploaded_at"]

    def __str__(self):
        return f"{self.client.name} / {self.file_name}"