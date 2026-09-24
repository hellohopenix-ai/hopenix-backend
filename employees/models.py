import uuid

from django.conf import settings
from django.db import models


# ---------------------------------------------------------------------------
# Backs EmployeesPage.jsx. The core identity of an "employee" (name, email,
# role, department, avatar, salary, approval status, join date) already
# lives on users.User / users.Profile and is managed from Users & Roles
# (UserPage.jsx) via AuthContext — this app deliberately does NOT duplicate
# any of that. It only stores the handful of fields that were previously
# kept in EmployeesPage.jsx's own localStorage and have no home anywhere
# else in the backend yet:
#   - the Active / On Leave employment status shown on this page (distinct
#     from users.User.status, which is the pending/approved/rejected/
#     deactivated *account approval* status)
#   - the 1-5 star performance rating
#   - a free-text location override
#   - manual project/task fallback counts, used only for an employee who
#     doesn't show up on any real projects.Project / tasks.Task yet
#   - leave requests, company holidays, and the promotion/bonus/post
#     announcement feed
# See employees/serializers.py:EmployeeSerializer for how this is merged
# back together with users.User at read time.
# ---------------------------------------------------------------------------


def announcement_image_path(instance, filename):
    return f"employees/announcements/{instance.id or 'new'}/image/{uuid.uuid4()}_{filename}"


def announcement_pdf_path(instance, filename):
    return f"employees/announcements/{instance.id or 'new'}/pdf/{uuid.uuid4()}_{filename}"


class EmployeeExtra(models.Model):
    """One row per employee, created on first write (get_or_create) —
    an employee with no row yet just gets the defaults below (Active
    status, 5-star rating, no manual fallback numbers)."""

    STATUS_CHOICES = [
        ("Active", "Active"),
        ("On Leave", "On Leave"),
    ]

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="employee_extra"
    )

    employment_status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="Active")
    rating = models.PositiveSmallIntegerField(default=5)
    location = models.CharField(max_length=255, blank=True, default="")

    # Fallback numbers only used while this employee has no matching
    # project (projects.Project.team / .manager) or task
    # (tasks.Task.assignees) yet — see EmployeeSerializer._project_stats /
    # _task_stats. Once they appear on a real project/task, the live count
    # takes over automatically and these are ignored (though still kept,
    # in case they're ever removed from every project/task again).
    manual_projects_assigned = models.PositiveIntegerField(default=0)
    manual_projects_completed = models.PositiveIntegerField(default=0)
    manual_tasks = models.PositiveIntegerField(default=0)
    manual_tasks_completed = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"EmployeeExtra({self.user.email})"


class LeaveRequest(models.Model):
    """A leave request filed against one employee. Only an "approved"
    request counts toward the monthly leave total EmployeesPage.jsx's
    payroll math uses — pending/rejected ones never affect it (same rule
    the old frontend-only version enforced client-side)."""

    TYPE_CHOICES = [
        ("Half Day", "Half Day"),
        ("Full Day", "Full Day"),
    ]
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("approved", "Approved"),
        ("rejected", "Rejected"),
    ]

    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="leave_requests"
    )
    type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    start_date = models.DateField()
    end_date = models.DateField()
    days = models.DecimalField(max_digits=5, decimal_places=1)
    reason = models.TextField(blank=True, default="")
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="pending")

    requested_at = models.DateTimeField(auto_now_add=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        ordering = ["-requested_at"]

    def __str__(self):
        return f"{self.employee.email} / {self.type} / {self.start_date}"


class Holiday(models.Model):
    """An admin-announced company holiday for one specific date. Sunday
    is always an automatic weekly off and is never stored here — see
    employees/views.py HolidayListCreateView, which rejects Sundays the
    same way EmployeesPage.jsx's announceHoliday() used to."""

    date = models.DateField(unique=True)
    reason = models.CharField(max_length=255, blank=True, default="Company Holiday")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["date"]

    def __str__(self):
        return f"{self.date} - {self.reason}"


class Announcement(models.Model):
    """One entry in the shared Promotions/Bonuses/Company-posts feed,
    visible to every employee. "promotion" and "bonus" are tied to one
    employee (and queue that employee's 🥳 congrats popup); "post" is a
    general announcement with no employee attached."""

    TYPE_CHOICES = [
        ("promotion", "Promotion"),
        ("bonus", "Bonus"),
        ("post", "Post"),
    ]

    type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="announcements"
    )
    detail = models.CharField(max_length=255, blank=True, default="")  # e.g. new job title / bonus amount
    message = models.TextField(blank=True, default="")

    image = models.FileField(upload_to=announcement_image_path, null=True, blank=True)
    pdf_file = models.FileField(upload_to=announcement_pdf_path, null=True, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    # ids/emails of whoever already got the 🥳 popup for this entry — see
    # AnnouncementSeenView / dismissCelebration() on the frontend.
    seen_by = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.type} -> {self.employee_id}"
