from rest_framework import generics, permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Availability, Meeting, MeetingRequest, RescheduleRequest
from .permissions import can_manage_meetings
from .serializers import (
    AvailabilitySerializer,
    MeetingRequestSerializer,
    MeetingRescheduleSerializer,
    MeetingSerializer,
    MeetingSetLinkSerializer,
    MeetingSetStatusSerializer,
    RescheduleRequestSerializer,
)


def _display_name(user):
    return getattr(user, "name", "") or getattr(user, "email", "") or "You"


class IsAuthenticatedReadManagerWrite(permissions.BasePermission):
    """List/retrieve: any authenticated user. Create/update/delete and
    every custom @action below: admin/manager (or someone individually
    granted "full" access to the Meetings page) only — mirrors
    AdminMeetingsView being the only place that calls createMeeting(),
    updateMeetingStatus(), rescheduleMeeting(), setMeetingLink()."""

    def has_permission(self, request, view):
        if not (request.user and request.user.is_authenticated):
            return False
        if view.action in ("list", "retrieve"):
            return True
        return can_manage_meetings(request.user)


class MeetingViewSet(viewsets.ModelViewSet):
    """Full CRUD for Meetings.jsx's `pm_meetings` store, plus the
    handful of actions the page needs beyond plain field edits — setting
    the Google Meet link, declining/cancelling (both are just a status
    change), and an admin/manager rescheduling a meeting directly.

    List visibility: admin/manager see every meeting (AdminMeetingsView).
    Everyone else only sees meetings they're actually a participant in —
    exactly what UserMeetings' `myMeetings` filter keeps for itself, just
    enforced server-side instead of trusting the client to only display
    the subset it was handed."""

    serializer_class = MeetingSerializer
    permission_classes = [IsAuthenticatedReadManagerWrite]

    def get_queryset(self):
        qs = Meeting.objects.all()
        user = self.request.user
        if not can_manage_meetings(user):
            qs = qs.filter(participants__contains=[_display_name(user)])

        params = self.request.query_params
        status_param = params.get("status")
        if status_param:
            qs = qs.filter(status=status_param)
        type_param = params.get("type")
        if type_param:
            qs = qs.filter(type=type_param)
        project = params.get("project")
        if project:
            qs = qs.filter(project=project)
        date_param = params.get("date")
        if date_param:
            qs = qs.filter(raw_date=date_param)

        return qs

    def perform_create(self, serializer):
        created_by = serializer.validated_data.get("created_by") or _display_name(self.request.user)
        serializer.save(created_by=created_by, created_by_user=self.request.user)

    @action(detail=True, methods=["post"], url_path="set-status")
    def set_status(self, request, pk=None):
        """POST /api/meetings/meetings/{id}/set-status/  body: {"status": "Declined"}
        Mirrors updateMeetingStatus(id, status) — used for both
        handleDecline() (-> "Declined") and handleCancelMeeting()
        (-> "Cancelled")."""

        meeting = self.get_object()
        body = MeetingSetStatusSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        meeting.status = body.validated_data["status"]
        meeting.save(update_fields=["status", "updated_at"])
        return Response(MeetingSerializer(meeting).data)

    @action(detail=True, methods=["post"], url_path="set-link")
    def set_link(self, request, pk=None):
        """POST /api/meetings/meetings/{id}/set-link/  body: {"meetLink": "https://..."}
        Mirrors setMeetingLink(id, meetLink)."""

        meeting = self.get_object()
        body = MeetingSetLinkSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        meeting.meet_link = body.validated_data["meetLink"].strip()
        meeting.save(update_fields=["meet_link", "updated_at"])
        return Response(MeetingSerializer(meeting).data)

    @action(detail=True, methods=["post"], url_path="reschedule")
    def reschedule(self, request, pk=None):
        """POST /api/meetings/meetings/{id}/reschedule/  body: {"rawDate": "...", "rawTime": "..."}
        Mirrors rescheduleMeeting(id, rawDate, rawTime) — an admin/manager
        moving one of their own meetings directly (not via a reschedule
        request). Slot-conflict checked the same way MeetingSerializer
        does for a plain create/update."""

        meeting = self.get_object()
        body = MeetingRescheduleSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        raw_date = body.validated_data["rawDate"]
        raw_time = body.validated_data["rawTime"]

        conflict = (
            Meeting.objects.filter(raw_date=raw_date, raw_time=raw_time)
            .exclude(status__in=["Declined", "Cancelled"])
            .exclude(pk=meeting.pk)
            .exists()
        )
        if conflict:
            return Response(
                {"detail": "There's already a meeting at this date and time — please choose a different slot."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        meeting.raw_date = raw_date
        meeting.raw_time = raw_time
        meeting.save(update_fields=["raw_date", "raw_time", "updated_at"])
        return Response(MeetingSerializer(meeting).data)


class MeetingRequestViewSet(viewsets.ModelViewSet):
    """Backs `pm_meeting_requests` — UserRequestModal's submitMeetingRequest()
    on the create side, RequestRow's onApprove/onReject on the review
    side. Any authenticated user can send a request and see their own;
    only admin/manager can see every request, or approve/reject one."""

    serializer_class = MeetingRequestSerializer
    http_method_names = ["get", "post", "head", "options"]

    def get_permissions(self):
        if self.action == "create":
            return [permissions.IsAuthenticated()]
        return [IsAuthenticatedReadManagerWrite()]

    def get_queryset(self):
        qs = MeetingRequest.objects.all()
        user = self.request.user
        if not can_manage_meetings(user):
            qs = qs.filter(requested_by=user)
        status_param = self.request.query_params.get("status")
        if status_param:
            qs = qs.filter(status=status_param)
        return qs

    def perform_create(self, serializer):
        # Mirrors submitMeetingRequest({name, role, project, reason,
        # rawDate, rawTime}) — name/role/org default from the logged-in
        # user so a client can't spoof who's asking.
        user = self.request.user
        name = serializer.validated_data.get("name") or _display_name(user)
        org = serializer.validated_data.get("org") or serializer.validated_data.get("project", "General")
        serializer.save(
            name=name,
            role=serializer.validated_data.get("role") or getattr(user, "role", ""),
            org=org,
            requested_by=user,
        )

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        """POST /api/meetings/requests/{id}/approve/
        Mirrors handleApprove(r): rejects with a 400 (same message the
        frontend shows) if the slot's already booked, otherwise creates
        the Meeting and marks this request approved."""

        req = self.get_object()
        if req.status != "pending":
            return Response({"detail": "This request has already been reviewed."}, status=400)

        conflict = Meeting.objects.filter(raw_date=req.raw_date, raw_time=req.raw_time).exclude(
            status__in=["Declined", "Cancelled"]
        ).exists()
        if conflict:
            return Response(
                {"detail": "This slot is already booked — please reschedule first."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        meeting_type = "Developer" if req.role == "Developer" else "Team" if req.role == "Team" else "Client"
        meeting = Meeting.objects.create(
            title=f"{req.role or 'Client'} Meeting — {req.project}",
            type=meeting_type,
            project=req.project,
            raw_date=req.raw_date,
            raw_time=req.raw_time,
            participants=[req.name, _display_name(request.user)],
            agenda=[req.reason] if req.reason else [],
            created_by=_display_name(request.user),
            created_by_user=request.user,
        )
        req.status = "approved"
        req.resulting_meeting = meeting
        req.save(update_fields=["status", "resulting_meeting"])
        return Response(MeetingRequestSerializer(req).data)

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        """POST /api/meetings/requests/{id}/reject/ — mirrors handleReject(id)."""

        req = self.get_object()
        if req.status != "pending":
            return Response({"detail": "This request has already been reviewed."}, status=400)
        req.status = "rejected"
        req.save(update_fields=["status"])
        return Response(MeetingRequestSerializer(req).data)


class RescheduleRequestViewSet(viewsets.ModelViewSet):
    """Backs `pm_reschedule_requests` — MeetingDetailsModal's
    onRequestReschedule() (submitRescheduleRequest) on the create side,
    AdminMeetingsView's handleApproveReschedule/handleRejectReschedule on
    the review side."""

    serializer_class = RescheduleRequestSerializer
    http_method_names = ["get", "post", "head", "options"]

    def get_permissions(self):
        if self.action == "create":
            return [permissions.IsAuthenticated()]
        return [IsAuthenticatedReadManagerWrite()]

    def get_queryset(self):
        qs = RescheduleRequest.objects.select_related("meeting").all()
        user = self.request.user
        if not can_manage_meetings(user):
            qs = qs.filter(requested_by=user)
        status_param = self.request.query_params.get("status")
        if status_param:
            qs = qs.filter(status=status_param)
        return qs

    def perform_create(self, serializer):
        user = self.request.user
        meeting = serializer.validated_data["meeting"]
        # A regular user may only request a reschedule for a meeting
        # they're actually part of — mirrors the fact that
        # MeetingDetailsModal's reschedule button only ever appears on a
        # meeting the current view already scoped to that user.
        if not can_manage_meetings(user) and _display_name(user) not in (meeting.participants or []):
            raise PermissionDenied("You can only request to reschedule a meeting you're part of.")
        name = serializer.validated_data.get("name") or _display_name(user)
        serializer.save(
            name=name,
            role=serializer.validated_data.get("role") or getattr(user, "role", ""),
            project=serializer.validated_data.get("project") or meeting.project,
            requested_by=user,
        )

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        """POST /api/meetings/reschedule-requests/{id}/approve/
        Mirrors handleApproveReschedule(req): moves the underlying
        Meeting's date/time (unless the target slot's now taken by
        something else) and marks this request approved."""

        req = self.get_object()
        if req.status != "pending":
            return Response({"detail": "This request has already been reviewed."}, status=400)

        conflict = (
            Meeting.objects.filter(raw_date=req.raw_date, raw_time=req.raw_time)
            .exclude(status__in=["Declined", "Cancelled"])
            .exclude(pk=req.meeting_id)
            .exists()
        )
        if conflict:
            return Response(
                {"detail": "This slot is already booked — reject or ask for a different time."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        meeting = req.meeting
        meeting.raw_date = req.raw_date
        meeting.raw_time = req.raw_time
        meeting.save(update_fields=["raw_date", "raw_time", "updated_at"])

        req.status = "approved"
        req.save(update_fields=["status"])
        return Response(RescheduleRequestSerializer(req).data)

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        """POST /api/meetings/reschedule-requests/{id}/reject/ — mirrors
        handleRejectReschedule(id)."""

        req = self.get_object()
        if req.status != "pending":
            return Response({"detail": "This request has already been reviewed."}, status=400)
        req.status = "rejected"
        req.save(update_fields=["status"])
        return Response(RescheduleRequestSerializer(req).data)


class AvailabilityView(APIView):
    """GET/PUT /api/meetings/availability/ — backs `pm_admin_availability`.
    Any authenticated user can read it (AvailabilityCard, read-only, on
    both the admin and user views); only admin/manager can change it
    (the "My Availability" edit form on AdminMeetingsView)."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        return Response(AvailabilitySerializer(Availability.load()).data)

    def put(self, request):
        if not can_manage_meetings(request.user):
            raise PermissionDenied("Only an admin or manager can update availability.")
        obj = Availability.load()
        serializer = AvailabilitySerializer(obj, data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)
