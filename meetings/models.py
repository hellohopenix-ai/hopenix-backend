from django.conf import settings
from django.db import models


class Meeting(models.Model):
    """Backs Meetings.jsx — every field here is named/shaped to match what
    that page's `pm_meetings` localStorage array already holds, so wiring
    the page up to this API is a drop-in (see MeetingSerializer for the
    camelCase mapping: rawDate/rawTime/meetLink/...).

    `participants` stays a plain JSON list of display-name strings (not a
    relation to `users.User`), same reasoning as Task.assignees in
    tasks/models.py: that's exactly what the frontend already stores and
    matches against everywhere (`m.participants.includes(user.name)`,
    MeetingCard's avatar stack, the Calendar's dot-per-day, ...) — turning
    that into a FK/M2M would be a real behaviour change, not just "give it
    a backend". The special "You" placeholder name the frontend seeds
    stays exactly as-is; the frontend is responsible for putting the
    logged-in user's real display name into this list when it creates or
    approves a meeting.

    `date` / `time` (the pretty "Sep 15, 2026" / "10:00 AM – 11:00 AM"
    strings shown in the UI) are deliberately NOT stored here — they're
    always derivable from rawDate/rawTime, exactly like TasksPage.jsx
    formats `dueDate` itself. Keep computing them client-side with the
    existing formatDatePretty()/formatTimeRangePretty() helpers.
    """

    TYPE_CHOICES = [
        ("Client", "Client"),
        ("Team", "Team"),
        ("Developer", "Developer"),
    ]

    STATUS_CHOICES = [
        ("Upcoming", "Upcoming"),
        ("Confirmed", "Confirmed"),
        ("In Progress", "In Progress"),
        ("Completed", "Completed"),
        ("Cancelled", "Cancelled"),
        ("Declined", "Declined"),
    ]

    title = models.CharField(max_length=255)
    type = models.CharField(max_length=20, choices=TYPE_CHOICES, default="Team")
    project = models.CharField(max_length=255)

    raw_date = models.DateField()
    raw_time = models.TimeField()

    # Up to any number of names — matches m.participants everywhere.
    participants = models.JSONField(default=list, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="Upcoming")
    # List of agenda-point strings — matches m.agenda.map(...) in
    # MeetingDetailsModal.
    agenda = models.JSONField(default=list, blank=True)
    meet_link = models.CharField(max_length=1000, blank=True)

    created_by = models.CharField(max_length=150, blank=True)
    created_by_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_meetings",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.title} — {self.raw_date} {self.raw_time}"

    def is_open_slot_blocker(self):
        """A meeting still "holds" its (date, time) slot unless it's been
        Declined or Cancelled — mirrors isSlotTaken()'s filter exactly."""
        return self.status not in ("Declined", "Cancelled")


class MeetingRequest(models.Model):
    """Backs the `pm_meeting_requests` store — a non-admin asking for a
    meeting to be scheduled. Approving one creates a Meeting (see
    MeetingRequestViewSet.approve); it does not become one automatically."""

    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("approved", "Approved"),
        ("rejected", "Rejected"),
    ]

    name = models.CharField(max_length=150)
    role = models.CharField(max_length=50, blank=True)
    # org mirrors project (submitMeetingRequest sets both to the same
    # value on the frontend) — kept as its own column for an exact
    # field-for-field match with the stored request object.
    org = models.CharField(max_length=255, blank=True)
    project = models.CharField(max_length=255)
    reason = models.TextField(blank=True)

    raw_date = models.DateField()
    raw_time = models.TimeField()
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="pending")

    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="meeting_requests",
    )
    # Set once approve() creates the meeting, so the frontend (or admin)
    # can jump straight to it if needed.
    resulting_meeting = models.ForeignKey(
        Meeting, on_delete=models.SET_NULL, null=True, blank=True, related_name="from_request"
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.name} — {self.project} ({self.status})"


class RescheduleRequest(models.Model):
    """Backs `pm_reschedule_requests`. A user can't reschedule a meeting
    directly — only request a new date/time; an admin/manager approving
    it is what actually moves the Meeting (see
    RescheduleRequestViewSet.approve)."""

    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("approved", "Approved"),
        ("rejected", "Rejected"),
    ]

    meeting = models.ForeignKey(Meeting, on_delete=models.CASCADE, related_name="reschedule_requests")
    name = models.CharField(max_length=150)
    role = models.CharField(max_length=50, blank=True)
    project = models.CharField(max_length=255, blank=True)
    reason = models.TextField(blank=True)

    raw_date = models.DateField()
    raw_time = models.TimeField()
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="pending")

    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reschedule_requests",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"Reschedule for meeting #{self.meeting_id} ({self.status})"


class Availability(models.Model):
    """Backs `pm_admin_availability` — one shared row that the
    admin/manager "My Availability" card edits and everyone else's
    <AvailabilityCard title="Admin Availability" /> reads read-only.
    Deliberately a singleton: always loaded/saved through
    Availability.load(), which get_or_create's the one row with pk=1
    instead of the frontend's "one localStorage key" trick."""

    WEEK_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    active = models.BooleanField(default=True)
    start_time = models.TimeField(default="09:00")
    end_time = models.TimeField(default="18:00")
    days = models.JSONField(default=list, blank=True)

    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Availability ({'active' if self.active else 'inactive'})"

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(
            pk=1,
            defaults={
                "active": True,
                "start_time": "09:00",
                "end_time": "18:00",
                "days": ["Mon", "Tue", "Wed", "Thu", "Fri"],
            },
        )
        return obj
