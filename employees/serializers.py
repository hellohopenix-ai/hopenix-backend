from django.contrib.auth import get_user_model
from django.db.models import Q
from rest_framework import serializers

from projects.models import Project
from tasks.models import Task

from .models import Announcement, EmployeeExtra, Holiday, LeaveRequest
from .permissions import is_admin

User = get_user_model()

ROLE_DISPLAY = {
    "employee": "Employee",
    "manager": "Manager",
    "accountant": "Accountant",
}


def display_role_for(role):
    """Mirrors the frontend's displayRoleFor() exactly, so `role` on an
    employee record looks identical whether it came from the old
    localStorage copy or straight from this API."""
    if role in ROLE_DISPLAY:
        return ROLE_DISPLAY[role]
    if not role:
        return "Employee"
    return role[0].upper() + role[1:]


def project_stats_for(user):
    """(assigned, completed) real project counts for this user, straight
    out of projects.Project — same identity ProjectsPage/the projects API
    itself uses (team membership or being the manager)."""
    qs = Project.objects.filter(Q(team=user) | Q(manager=user)).distinct()
    assigned = qs.count()
    completed = qs.filter(status="Completed").count()
    return assigned, completed


def task_stats_for(user):
    """(total, done) real task counts for this user out of tasks.Task.
    Task.assignees is a plain JSON list of display-name strings (see
    tasks/models.py), not a relation, so we match by name the same way
    TasksPage.jsx's getAssignees()/resolveRealAssignee() does — a coarse
    `icontains` cuts the candidate set at the DB level, then an exact
    case-insensitive comparison in Python avoids false positives from
    partial name matches."""
    name = (user.name or "").strip()
    if not name:
        return 0, 0
    needle = name.lower()
    candidates = Task.objects.filter(assignees__icontains=name).only("assignees", "status")
    mine = [t for t in candidates if any((a or "").strip().lower() == needle for a in (t.assignees or []))]
    total = len(mine)
    done = sum(1 for t in mine if t.status == "Completed")
    return total, done


class LeaveRequestSerializer(serializers.ModelSerializer):
    """Shape matches the leaveRequests[] entries EmployeesPage.jsx already
    builds locally: { id, type, startDate, endDate, days, reason, status,
    requestedAt, decidedAt }."""

    startDate = serializers.DateField(source="start_date")
    endDate = serializers.DateField(source="end_date", required=False)
    requestedAt = serializers.DateTimeField(source="requested_at", read_only=True)
    decidedAt = serializers.DateTimeField(source="decided_at", read_only=True)
    employeeId = serializers.IntegerField(source="employee_id", read_only=True)

    class Meta:
        model = LeaveRequest
        fields = [
            "id", "employeeId", "type", "startDate", "endDate", "days",
            "reason", "status", "requestedAt", "decidedAt",
        ]
        read_only_fields = ["days", "status"]

    def validate(self, attrs):
        leave_type = attrs.get("type")
        start = attrs.get("start_date")
        end = attrs.get("end_date") or start

        if leave_type == "Half Day":
            end = start
            days = 0.5
        else:
            if end < start:
                raise serializers.ValidationError({"endDate": "End date can't be before the start date."})
            days = (end - start).days + 1

        attrs["end_date"] = end
        attrs["days"] = days
        return attrs


class HolidaySerializer(serializers.ModelSerializer):
    class Meta:
        model = Holiday
        fields = ["id", "date", "reason"]

    def validate_date(self, value):
        if value.weekday() == 6:  # Sunday — 6 in Python's Mon=0..Sun=6
            raise serializers.ValidationError("Sunday is already an automatic weekly off.")
        return value


class AnnouncementSerializer(serializers.ModelSerializer):
    """Shape matches the announcement entries EmployeesPage.jsx builds in
    giveRecognition(): { id, type, employeeId, employeeName, detail,
    message, createdAt, seenBy, image, pdfFile }."""

    employeeId = serializers.PrimaryKeyRelatedField(
        source="employee", queryset=User.objects.all(), required=False, allow_null=True
    )
    employeeName = serializers.CharField(source="employee.name", read_only=True, default=None)
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)
    seenBy = serializers.JSONField(source="seen_by", read_only=True)
    image = serializers.SerializerMethodField()
    pdfFile = serializers.SerializerMethodField()

    class Meta:
        model = Announcement
        fields = [
            "id", "type", "employeeId", "employeeName", "detail", "message",
            "createdAt", "seenBy", "image", "pdfFile",
        ]

    def _file_url(self, field_file):
        if not field_file:
            return None
        request = self.context.get("request")
        url = field_file.url
        return request.build_absolute_uri(url) if request else url

    def get_image(self, obj):
        return self._file_url(obj.image)

    def get_pdfFile(self, obj):
        return self._file_url(obj.pdf_file)

    def validate(self, attrs):
        entry_type = attrs.get("type")
        if entry_type == "post":
            if not (attrs.get("message") or "").strip():
                raise serializers.ValidationError({"message": "A post needs a message."})
        elif not attrs.get("employee"):
            raise serializers.ValidationError({"employeeId": "Pick an employee for a promotion/bonus."})
        return attrs


class EmployeeSerializer(serializers.ModelSerializer):
    """The full merged employee object EmployeesPage.jsx's table/cards/
    profile sections read — combining users.User (identity, already
    managed from Users & Roles), users.Profile (phone/salary/city/
    country), this app's EmployeeExtra (status/rating/location/manual
    fallback counts), live project/task stats computed straight from the
    real projects/tasks apps, and this employee's leave requests."""

    authId = serializers.IntegerField(source="id", read_only=True)
    joined = serializers.SerializerMethodField()
    phone = serializers.SerializerMethodField()
    salary = serializers.SerializerMethodField()
    avatar = serializers.SerializerMethodField()
    role = serializers.SerializerMethodField()
    status = serializers.SerializerMethodField()
    rating = serializers.SerializerMethodField()
    location = serializers.SerializerMethodField()
    dateOfBirth = serializers.SerializerMethodField()
    leaveRequests = serializers.SerializerMethodField()
    projectsAssigned = serializers.SerializerMethodField()
    projectsCompleted = serializers.SerializerMethodField()
    projectsRemaining = serializers.SerializerMethodField()
    tasks = serializers.SerializerMethodField()
    tasksCompleted = serializers.SerializerMethodField()
    tasksRemaining = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id", "authId", "name", "email", "department", "role", "status",
            "phone", "location", "avatar", "joined", "dateOfBirth", "rating",
            "salary", "projectsAssigned", "projectsCompleted", "projectsRemaining",
            "tasks", "tasksCompleted", "tasksRemaining", "leaveRequests",
        ]

    # -- small helpers ----------------------------------------------------

    def _profile(self, obj):
        return getattr(obj, "profile", None)

    def _extra(self, obj):
        return getattr(obj, "employee_extra", None)

    def _project_stats(self, obj):
        if not hasattr(obj, "_ep_project_stats"):
            assigned, completed = project_stats_for(obj)
            if assigned == 0:
                extra = self._extra(obj)
                if extra:
                    assigned = extra.manual_projects_assigned
                    completed = extra.manual_projects_completed
            obj._ep_project_stats = (assigned, completed)
        return obj._ep_project_stats

    def _task_stats(self, obj):
        if not hasattr(obj, "_ep_task_stats"):
            total, done = task_stats_for(obj)
            if total == 0:
                extra = self._extra(obj)
                if extra:
                    total = extra.manual_tasks
                    done = extra.manual_tasks_completed
            obj._ep_task_stats = (total, done)
        return obj._ep_task_stats

    # -- fields -------------------------------------------------------------

    def get_role(self, obj):
        return display_role_for(obj.role)

    def get_joined(self, obj):
        return obj.date_joined.date().isoformat() if obj.date_joined else None

    def get_dateOfBirth(self, obj):
        profile = self._profile(obj)
        return profile.dob.isoformat() if profile and profile.dob else ""

    def get_phone(self, obj):
        profile = self._profile(obj)
        return (profile.phone if profile else "") or ""

    def get_salary(self, obj):
        # Sensitive figure: only an admin, or the employee viewing their
        # own row, ever gets the real number back. Everyone else previously
        # received it in the raw API response and relied on the frontend
        # hiding the column -- that was a server-side leak, so it's masked
        # here now regardless of what the client does with it.
        request = self.context.get("request")
        user = getattr(request, "user", None)
        is_self = bool(user and user.is_authenticated and user.pk == obj.pk)
        if not (is_self or is_admin(user)):
            return None
        profile = self._profile(obj)
        return profile.salary if profile else None

    def get_avatar(self, obj):
        request = self.context.get("request")
        profile = self._profile(obj)
        source = obj.avatar or (profile.profile_photo if profile else None)
        if not source:
            return None
        url = source.url
        return request.build_absolute_uri(url) if request else url

    def get_status(self, obj):
        extra = self._extra(obj)
        return extra.employment_status if extra else "Active"

    def get_rating(self, obj):
        extra = self._extra(obj)
        rating = extra.rating if extra else 5
        return min(5, max(1, rating))

    def get_location(self, obj):
        extra = self._extra(obj)
        if extra and extra.location:
            return extra.location
        profile = self._profile(obj)
        if profile and (profile.city or profile.country):
            return ", ".join(p for p in [profile.city, profile.country] if p)
        return ""

    def get_leaveRequests(self, obj):
        return LeaveRequestSerializer(obj.leave_requests.all(), many=True).data

    def get_projectsAssigned(self, obj):
        return self._project_stats(obj)[0]

    def get_projectsCompleted(self, obj):
        return self._project_stats(obj)[1]

    def get_projectsRemaining(self, obj):
        assigned, completed = self._project_stats(obj)
        return max(0, assigned - completed)

    def get_tasks(self, obj):
        return self._task_stats(obj)[0]

    def get_tasksCompleted(self, obj):
        return self._task_stats(obj)[1]

    def get_tasksRemaining(self, obj):
        total, done = self._task_stats(obj)
        return max(0, total - done)