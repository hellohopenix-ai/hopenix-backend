from django.conf import settings
from django.db import models
from django.utils import timezone


class VisitorPurpose(models.TextChoices):
    """Mirrors VisitorsPage.jsx's PURPOSE_OPTIONS exactly, so the
    dropdown's values are always valid choices here too."""

    BUSINESS_MEETING = "Business Meeting", "Business Meeting"
    INTERVIEW = "Interview", "Interview"
    DELIVERY_VENDOR = "Delivery / Vendor", "Delivery / Vendor"
    PERSONAL_VISIT = "Personal Visit", "Personal Visit"
    OTHER = "Other", "Other"


class AppointmentStatus(models.TextChoices):
    """Mirrors VisitorsPage.jsx's APPOINTMENT_STATUS_OPTIONS."""

    SCHEDULED = "Scheduled", "Scheduled"
    WALK_IN = "Walk-in", "Walk-in"
    CONFIRMED = "Confirmed", "Confirmed"


class VisitorStatus(models.TextChoices):
    WAITING = "Waiting", "Waiting"
    APPROVED = "Approved", "Approved"
    REJECTED = "Rejected", "Rejected"


class Visitor(models.Model):
    """Backs VisitorsPage.jsx. Field names/shape are chosen so the page's
    existing visitor object (name, phone, cnic, company, meetingWith,
    purpose, purposeNote, apptStatus, status, reviewed, visits, date,
    requestedAt) maps onto this with only the localStorage read/write
    swapped for real API calls — same drop-in approach expenses.Expense
    and tasks.Task already use elsewhere in this backend.

    Approval is admin-only (enforced in visitors.permissions /
    VisitorViewSet, not just hidden in the UI) regardless of who
    `meeting_with` names — the frontend only ever lets an "admin" role
    see/use the Host Approval panel, so that's the audience a new
    request's push notification goes to as well (see views.py).
    """

    # Human-facing id shown throughout the frontend (e.g. "HV-20250422-001")
    # — kept as a separate field rather than the real pk so it can follow
    # the existing "HV-<date>-<seq>" convention exactly; `id` (the actual
    # pk) is still what every FK / URL lookup uses.
    code = models.CharField(max_length=32, unique=True, editable=False)

    name = models.CharField(max_length=255)
    phone = models.CharField(max_length=32)
    cnic = models.CharField(max_length=32, blank=True, default="")
    company = models.CharField(max_length=255, blank=True, default="")

    # Plain text, not a FK to User — the frontend's host picker already
    # stores the selected "Name — Role" label as the value (see
    # VisitorsPage.jsx's hostOptions/`<option value={h.label}>`), and
    # every admin (not just the named host) is the one who actually
    # approves, so nothing here needs to resolve back to a specific
    # account.
    meeting_with = models.CharField(max_length=255, blank=True, default="")

    purpose = models.CharField(max_length=32, choices=VisitorPurpose.choices, blank=True, default="")
    purpose_note = models.TextField(blank=True, default="")
    appt_status = models.CharField(
        max_length=16, choices=AppointmentStatus.choices, default=AppointmentStatus.SCHEDULED
    )

    status = models.CharField(max_length=10, choices=VisitorStatus.choices, default=VisitorStatus.WAITING)
    # A Waiting entry is "reviewed" once an admin has either decided it
    # (Approved/Rejected) or explicitly asked the visitor to wait — either
    # way it drops out of the live "Host Approval" queue. Re-checking the
    # "Review" action on an already-reviewed Waiting row (see
    # VisitorViewSet.reopen) flips this back to False to bring it back up.
    reviewed = models.BooleanField(default=False)

    # Snapshotted at registration time (how many prior visits this same
    # phone number has on file, +1) — matches the frontend's own
    # `priorVisits + 1` at submit time, rather than a live COUNT that
    # would make old records silently change as new ones come in.
    visits = models.PositiveIntegerField(default=1)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="registered_visitors"
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["phone"]),
        ]

    def __str__(self):
        return f"{self.code} — {self.name} ({self.status})"

    def save(self, *args, **kwargs):
        if not self.code:
            self.code = self._generate_code()
        super().save(*args, **kwargs)

    def _generate_code(self):
        # HV-<YYYYMMDD>-<seq>, seq resetting each day — same shape as the
        # frontend's own `HV-${date}-${visitors.length + 1}` id, just
        # counted from the real table instead of an in-memory array so it
        # stays correct with concurrent front-desk users.
        today = timezone.localdate()
        prefix = f"HV-{today.strftime('%Y%m%d')}-"
        last = (
            Visitor.objects.filter(code__startswith=prefix)
            .order_by("-code")
            .values_list("code", flat=True)
            .first()
        )
        seq = int(last.rsplit("-", 1)[1]) + 1 if last else 1
        return f"{prefix}{seq:03d}"
