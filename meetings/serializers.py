from rest_framework import serializers

from .models import Availability, Meeting, MeetingRequest, RescheduleRequest


class MeetingSerializer(serializers.ModelSerializer):
    """Field names here are deliberately camelCase (rawDate, rawTime,
    meetLink, createdBy, ...) so this is a drop-in JSON shape for
    Meetings.jsx — everywhere that page does `m.rawDate`, `m.meetLink`,
    etc. reads straight off what this serializer returns, same as
    TaskSerializer does for TasksPage.jsx. The pretty `date`/`time`
    strings are NOT included — compute them client-side with
    formatDatePretty()/formatTimeRangePretty(), same as before."""

    rawDate = serializers.DateField(source="raw_date")
    rawTime = serializers.TimeField(source="raw_time", format="%H:%M", input_formats=["%H:%M", "%H:%M:%S"])
    meetLink = serializers.CharField(source="meet_link", required=False, allow_blank=True)
    createdBy = serializers.CharField(source="created_by", required=False, allow_blank=True)
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)
    updatedAt = serializers.DateTimeField(source="updated_at", read_only=True)

    class Meta:
        model = Meeting
        fields = [
            "id", "title", "type", "project",
            "rawDate", "rawTime",
            "participants", "status", "agenda", "meetLink",
            "createdBy", "createdAt", "updatedAt",
        ]

    def validate_participants(self, value):
        if not isinstance(value, list):
            raise serializers.ValidationError("participants must be a list of names.")
        return value

    def validate_agenda(self, value):
        if not isinstance(value, list):
            raise serializers.ValidationError("agenda must be a list of strings.")
        return value

    def validate(self, attrs):
        # Mirrors isSlotTaken(rawDate, rawTime, excludeId) — only 1 live
        # meeting per (date, start time). On update, fall back to the
        # instance's current value for whichever field wasn't sent.
        raw_date = attrs.get("raw_date", getattr(self.instance, "raw_date", None))
        raw_time = attrs.get("raw_time", getattr(self.instance, "raw_time", None))
        if raw_date and raw_time:
            qs = Meeting.objects.filter(raw_date=raw_date, raw_time=raw_time).exclude(
                status__in=["Declined", "Cancelled"]
            )
            if self.instance is not None:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise serializers.ValidationError(
                    {"detail": "There's already a meeting at this date and time — please choose a different slot."}
                )
        return attrs


class MeetingRescheduleSerializer(serializers.Serializer):
    """POST body for /api/meetings/meetings/{id}/reschedule/ — mirrors
    rescheduleMeeting(id, rawDate, rawTime), used when an admin/manager
    moves one of their own meetings directly (not via a reschedule
    request)."""

    rawDate = serializers.DateField()
    rawTime = serializers.TimeField(format="%H:%M", input_formats=["%H:%M", "%H:%M:%S"])


class MeetingSetLinkSerializer(serializers.Serializer):
    """POST body for /api/meetings/meetings/{id}/set-link/ — mirrors
    setMeetingLink(id, meetLink)."""

    meetLink = serializers.CharField(allow_blank=True)


class MeetingSetStatusSerializer(serializers.Serializer):
    """POST body for /api/meetings/meetings/{id}/set-status/ — mirrors
    updateMeetingStatus(id, status), which both handleDecline() and
    handleCancelMeeting() call on the frontend."""

    status = serializers.ChoiceField(choices=Meeting.STATUS_CHOICES)


class MeetingRequestSerializer(serializers.ModelSerializer):
    # name/role/org are all optional on input — perform_create() fills
    # in whatever's missing from the logged-in user, same as
    # `name: user.name` defaulting in submitMeetingRequest() on the
    # frontend. Kept as plain writable fields (not read_only) so an
    # admin creating a request on someone's behalf can still set them.
    name = serializers.CharField(required=False, allow_blank=True)
    role = serializers.CharField(required=False, allow_blank=True)
    org = serializers.CharField(required=False, allow_blank=True)
    rawDate = serializers.DateField(source="raw_date")
    rawTime = serializers.TimeField(source="raw_time", format="%H:%M", input_formats=["%H:%M", "%H:%M:%S"])
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)
    resultingMeetingId = serializers.PrimaryKeyRelatedField(source="resulting_meeting", read_only=True)

    class Meta:
        model = MeetingRequest
        fields = [
            "id", "name", "role", "org", "project", "reason",
            "rawDate", "rawTime", "status", "createdAt", "resultingMeetingId",
        ]
        read_only_fields = ["status"]


class RescheduleRequestSerializer(serializers.ModelSerializer):
    meetingId = serializers.PrimaryKeyRelatedField(source="meeting", queryset=Meeting.objects.all())
    # name/role/project all optional on input — perform_create() fills
    # in whatever's missing from the logged-in user / the meeting being
    # rescheduled, same as submitRescheduleRequest()'s defaulting on the
    # frontend.
    name = serializers.CharField(required=False, allow_blank=True)
    role = serializers.CharField(required=False, allow_blank=True)
    project = serializers.CharField(required=False, allow_blank=True)
    rawDate = serializers.DateField(source="raw_date")
    rawTime = serializers.TimeField(source="raw_time", format="%H:%M", input_formats=["%H:%M", "%H:%M:%S"])
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)

    class Meta:
        model = RescheduleRequest
        fields = [
            "id", "meetingId", "name", "role", "project", "reason",
            "rawDate", "rawTime", "status", "createdAt",
        ]
        read_only_fields = ["status"]


class AvailabilitySerializer(serializers.ModelSerializer):
    startTime = serializers.TimeField(source="start_time", format="%H:%M", input_formats=["%H:%M", "%H:%M:%S"])
    endTime = serializers.TimeField(source="end_time", format="%H:%M", input_formats=["%H:%M", "%H:%M:%S"])

    class Meta:
        model = Availability
        fields = ["active", "startTime", "endTime", "days"]

    def validate_days(self, value):
        if not isinstance(value, list) or any(d not in Availability.WEEK_DAYS for d in value):
            raise serializers.ValidationError(f"days must be a subset of {Availability.WEEK_DAYS}.")
        return value
