from django.contrib.auth import get_user_model
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Announcement, EmployeeExtra, Holiday, LeaveRequest
from .permissions import IsAdmin, IsStaff, can_manage_leave_request, is_admin
from .serializers import (
    AnnouncementSerializer,
    EmployeeSerializer,
    HolidaySerializer,
    LeaveRequestSerializer,
)

User = get_user_model()


def _employee_queryset():
    """Every real employee EmployeesPage.jsx shows: an approved account
    that isn't Admin or Client — same filter the frontend's own sync
    effect applies to AuthContext's `approvedUsers`."""
    return (
        User.objects.filter(status="approved")
        .exclude(role__in=["admin", "client"])
        .select_related("profile", "employee_extra")
        .prefetch_related("leave_requests")
        .order_by("-date_joined")
    )


def _get_or_create_extra(user):
    extra, _ = EmployeeExtra.objects.get_or_create(user=user)
    return extra


def _serialize_employee(request, user):
    # Re-fetch through the same queryset so select_related/prefetch_related
    # stay in effect and the response shape never drifts from the list view.
    user = _employee_queryset().get(pk=user.pk)
    return EmployeeSerializer(user, context={"request": request}).data


# ---------------------------------------------------------------------------
# Employees list / single record
# ---------------------------------------------------------------------------


class EmployeeListView(APIView):
    """GET /api/employees/
    Every approved, non-admin/non-client account, merged with this app's
    rating/status/location/leave-requests and live project & task counts.
    Readable by any logged-in STAFF member (not the Client Portal role) —
    EmployeesPage.jsx shows the whole table to everyone and only hides the
    Salary column per row on the frontend; the server now applies that same
    mask itself (see EmployeeSerializer.get_salary) rather than trusting the
    client to hide it. Adding a new employee is unchanged: it still goes
    through POST /api/auth/users/invite/ (users.InviteUserView) — a
    brand-new person only becomes a real "employee" once an admin approves
    them from Users & Roles."""

    permission_classes = [IsStaff]

    def get(self, request):
        data = EmployeeSerializer(_employee_queryset(), many=True, context={"request": request}).data
        return Response(data)


class EmployeeDetailView(APIView):
    permission_classes = [IsStaff]

    def get(self, request, user_id):
        user = get_object_or_404(_employee_queryset(), id=user_id)
        return Response(EmployeeSerializer(user, context={"request": request}).data)


class EmployeeStatusView(APIView):
    """POST /api/employees/<user_id>/status/  { status: "Active" | "On Leave" }
    Admin-only. This is the employment status shown on this page — separate
    from the account approval status in Users & Roles."""

    permission_classes = [IsAdmin]

    def post(self, request, user_id):
        user = get_object_or_404(User, id=user_id)
        new_status = request.data.get("status")
        if new_status not in dict(EmployeeExtra.STATUS_CHOICES):
            return Response({"error": "Invalid status."}, status=status.HTTP_400_BAD_REQUEST)

        extra = _get_or_create_extra(user)
        extra.employment_status = new_status
        extra.save(update_fields=["employment_status"])
        return Response(_serialize_employee(request, user))


class EmployeeRatingView(APIView):
    """POST /api/employees/<user_id>/rating/  { rating: 1-5 }
    Admin-only. Dropping to 1 star is a deliberate admin decision made on
    the frontend (remove vs. one more chance -> rating 2) — this endpoint
    just stores whatever rating it's given, 1 included."""

    permission_classes = [IsAdmin]

    def post(self, request, user_id):
        user = get_object_or_404(User, id=user_id)
        try:
            rating = int(request.data.get("rating"))
        except (TypeError, ValueError):
            return Response({"error": "Rating must be a number from 1 to 5."}, status=status.HTTP_400_BAD_REQUEST)
        if rating < 1 or rating > 5:
            return Response({"error": "Rating must be between 1 and 5."}, status=status.HTTP_400_BAD_REQUEST)

        extra = _get_or_create_extra(user)
        extra.rating = rating
        extra.save(update_fields=["rating"])
        return Response(_serialize_employee(request, user))


class EmployeePerformanceView(APIView):
    """POST /api/employees/<user_id>/performance/
    { projectsAssigned?, projectsCompleted?, tasks?, tasksCompleted? }
    Admin-only. Sets the manual fallback counts used ONLY while this
    employee has no matching real project/task yet — see
    EmployeeSerializer._project_stats / _task_stats."""

    permission_classes = [IsAdmin]

    FIELD_MAP = {
        "projectsAssigned": "manual_projects_assigned",
        "projectsCompleted": "manual_projects_completed",
        "tasks": "manual_tasks",
        "tasksCompleted": "manual_tasks_completed",
    }

    def post(self, request, user_id):
        user = get_object_or_404(User, id=user_id)
        extra = _get_or_create_extra(user)

        updated_fields = []
        for js_field, model_field in self.FIELD_MAP.items():
            if js_field not in request.data:
                continue
            try:
                value = max(0, round(float(request.data[js_field])))
            except (TypeError, ValueError):
                return Response({"error": f"Invalid value for {js_field}."}, status=status.HTTP_400_BAD_REQUEST)
            setattr(extra, model_field, value)
            updated_fields.append(model_field)

        if updated_fields:
            extra.save(update_fields=updated_fields)
        return Response(_serialize_employee(request, user))


class EmployeeLocationView(APIView):
    """POST /api/employees/<user_id>/location/  { location }
    Admin-only. Free-text location override (city, country) shown on the
    employee's row/profile — independent of Profile.city/country, which
    the employee themselves fills in via Complete Your Profile."""

    permission_classes = [IsAdmin]

    def post(self, request, user_id):
        user = get_object_or_404(User, id=user_id)
        extra = _get_or_create_extra(user)
        extra.location = (request.data.get("location") or "").strip()
        extra.save(update_fields=["location"])
        return Response(_serialize_employee(request, user))


# ---------------------------------------------------------------------------
# Leave requests
# ---------------------------------------------------------------------------


class LeaveRequestListCreateView(APIView):
    """GET  /api/employees/leave-requests/?employee=<id>  (omit for every
         pending/approved/rejected request across all employees, newest
         first — feeds the admin "Leave Requests" panel)
    POST /api/employees/leave-requests/  { employee, type, startDate,
         endDate?, reason } — files a new request for that employee.
         Anyone can file their own (employee == request.user.id); only
         an admin can file one on someone else's behalf.

    Non-admins can only ever see their OWN leave requests here, whatever
    `?employee=` says — only an admin (the "Leave Requests" panel) gets the
    company-wide list, matching can_manage_leave_request's same admin/self
    split used for cancelling."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        qs = LeaveRequest.objects.select_related("employee").all()
        if is_admin(request.user):
            employee_id = request.query_params.get("employee")
            if employee_id:
                qs = qs.filter(employee_id=employee_id)
        else:
            qs = qs.filter(employee_id=request.user.id)
        return Response(LeaveRequestSerializer(qs, many=True).data)

    def post(self, request):
        employee_id = request.data.get("employee") or request.data.get("employeeId")
        if not employee_id:
            return Response({"error": "employee is required."}, status=status.HTTP_400_BAD_REQUEST)

        employee = get_object_or_404(User, id=employee_id)
        if not is_admin(request.user) and employee.id != request.user.id:
            return Response(
                {"error": "You can only request leave for yourself."}, status=status.HTTP_403_FORBIDDEN
            )

        serializer = LeaveRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        leave = serializer.save(employee=employee, status="pending")
        return Response(LeaveRequestSerializer(leave).data, status=status.HTTP_201_CREATED)


class LeaveRequestDecideView(APIView):
    """POST /api/employees/leave-requests/<id>/approve/
    POST /api/employees/leave-requests/<id>/reject/
    Admin-only. Approving is what actually counts those days toward the
    employee's monthly leave total (payroll math on the frontend reads
    only "approved" requests) — rejecting leaves it untouched. If the
    approved range covers today, the employee's Active/On Leave status is
    flipped to On Leave immediately."""

    permission_classes = [IsAdmin]

    def post(self, request, leave_id, decision):
        if decision not in ("approve", "reject"):
            return Response({"error": "Unknown decision."}, status=status.HTTP_400_BAD_REQUEST)

        leave = get_object_or_404(LeaveRequest, id=leave_id)
        if leave.status != "pending":
            return Response({"error": "This request has already been decided."}, status=status.HTTP_400_BAD_REQUEST)

        leave.status = "approved" if decision == "approve" else "rejected"
        leave.decided_at = timezone.now()
        leave.decided_by = request.user
        leave.save(update_fields=["status", "decided_at", "decided_by"])

        if leave.status == "approved":
            today = timezone.localdate()
            if leave.start_date <= today <= leave.end_date:
                extra = _get_or_create_extra(leave.employee)
                extra.employment_status = "On Leave"
                extra.save(update_fields=["employment_status"])

        return Response(LeaveRequestSerializer(leave).data)


class LeaveRequestCancelView(APIView):
    """DELETE /api/employees/leave-requests/<id>/
    The employee who filed a still-pending request can cancel it
    themselves; admin can cancel any pending request too."""

    permission_classes = [permissions.IsAuthenticated]

    def delete(self, request, leave_id):
        leave = get_object_or_404(LeaveRequest, id=leave_id)
        if not can_manage_leave_request(request.user, leave):
            return Response({"error": "You can't cancel this request."}, status=status.HTTP_403_FORBIDDEN)
        if leave.status != "pending":
            return Response({"error": "Only a pending request can be cancelled."}, status=status.HTTP_400_BAD_REQUEST)
        leave.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Holidays
# ---------------------------------------------------------------------------


class HolidayListCreateView(APIView):
    """GET  /api/employees/holidays/  — every announced holiday.
    POST /api/employees/holidays/  { date, reason }  — admin-only.
    Announcing again for a date that's already announced replaces its
    reason instead of creating a duplicate (same as the old
    announceHoliday()). Sunday is rejected — it's already automatic."""

    def get_permissions(self):
        if self.request.method == "POST":
            return [IsAdmin()]
        return [permissions.IsAuthenticated()]

    def get(self, request):
        return Response(HolidaySerializer(Holiday.objects.all(), many=True).data)

    def post(self, request):
        existing = Holiday.objects.filter(date=request.data.get("date")).first()
        serializer = HolidaySerializer(instance=existing, data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save(created_by=request.user)
        return Response(serializer.data, status=status.HTTP_201_CREATED if not existing else status.HTTP_200_OK)


class HolidayDeleteView(APIView):
    permission_classes = [IsAdmin]

    def delete(self, request, holiday_id):
        holiday = get_object_or_404(Holiday, id=holiday_id)
        holiday.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Announcements (promotions / bonuses / company posts)
# ---------------------------------------------------------------------------


class AnnouncementListCreateView(APIView):
    """GET  /api/employees/announcements/  — the shared feed, newest first.
    POST /api/employees/announcements/  { type, employeeId?, detail?,
         message?, image?, pdfFile? }  — admin-only. Accepts multipart so
         the image/PDF can be uploaded as real files (stored on disk under
         MEDIA_ROOT, same pattern as projects/expenses attachments — no
         more base64 data URLs in the payload)."""

    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get_permissions(self):
        if self.request.method == "POST":
            return [IsAdmin()]
        return [permissions.IsAuthenticated()]

    def get(self, request):
        qs = Announcement.objects.select_related("employee").all()
        return Response(AnnouncementSerializer(qs, many=True, context={"request": request}).data)

    def post(self, request):
        data = request.data.copy()
        # employeeId is sent as "" for a general Post — PrimaryKeyRelatedField
        # rejects an empty string outright, so normalize it to "not sent".
        if data.get("employeeId") in ("", "null", None):
            data.pop("employeeId", None)
        if data.get("type") != "promotion":
            data.pop("pdfFile", None)

        serializer = AnnouncementSerializer(data=data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        announcement = serializer.save(created_by=request.user, image=data.get("image"), pdf_file=data.get("pdfFile"))
        return Response(
            AnnouncementSerializer(announcement, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


class AnnouncementDeleteView(APIView):
    permission_classes = [IsAdmin]

    def delete(self, request, announcement_id):
        announcement = get_object_or_404(Announcement, id=announcement_id)
        announcement.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class AnnouncementSeenView(APIView):
    """POST /api/employees/announcements/<id>/seen/
    Marks this announcement as seen by the current user, so their 🥳
    congrats popup doesn't reappear on the next visit — mirrors
    dismissCelebration()'s identity key (auth id first, email fallback)."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, announcement_id):
        announcement = get_object_or_404(Announcement, id=announcement_id)
        key = str(request.user.id)
        seen_by = announcement.seen_by or []
        if key not in seen_by:
            seen_by.append(key)
            announcement.seen_by = seen_by
            announcement.save(update_fields=["seen_by"])
        return Response(AnnouncementSerializer(announcement, context={"request": request}).data)