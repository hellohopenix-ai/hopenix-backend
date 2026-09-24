from django.utils import timezone
from rest_framework import serializers

from .models import Visitor
from .permissions import can_approve_visitors


def _relative_time(dt):
    """"Just now" / "5 minutes ago" / "3 hours ago" / falls back to a
    plain date once it's more than a day old — matches the informal style
    VisitorsPage.jsx's seed data already used for `requestedAt` (e.g.
    "Just now"), computed for real instead of hand-typed."""
    seconds = (timezone.now() - dt).total_seconds()
    if seconds < 60:
        return "Just now"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    hours = int(minutes // 60)
    if hours < 24:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = int(hours // 24)
    if days < 7:
        return f"{days} day{'s' if days != 1 else ''} ago"
    return timezone.localtime(dt).strftime("%d %b %Y")


def _display_date(dt):
    """"22 Apr 2025, 11:24 AM" — same format VisitorsPage.jsx already
    builds client-side with toLocaleString({day:"2-digit", month:"short",
    year:"numeric", hour:"numeric", minute:"2-digit"}). Python's %I always
    zero-pads the hour (JS's hour:"numeric" doesn't), so that leading zero
    is stripped to match exactly."""
    local = timezone.localtime(dt)
    formatted = local.strftime("%d %b %Y, %I:%M %p")
    day, rest = formatted.split(" ", 1)
    time_part = formatted.split(", ")[1]
    if time_part.startswith("0"):
        formatted = formatted.replace(f", {time_part}", f", {time_part[1:]}")
    return formatted


class VisitorSerializer(serializers.ModelSerializer):
    """camelCase output so this is a drop-in JSON shape for
    VisitorsPage.jsx — same pattern as expenses.ExpenseSerializer /
    tasks.TaskSerializer elsewhere in this project.

    `id` is the human-facing code (e.g. "HV-20250422-001"), not the
    numeric database pk — VisitorViewSet looks records up by this same
    code (lookup_field="code"), so every place the frontend already
    treats `visitor.id` as the identifier (list keys, decide(id, ...),
    askToWait(id), reopenForReview(id)) keeps working unchanged.
    """

    id = serializers.CharField(source="code", read_only=True)
    meetingWith = serializers.CharField(source="meeting_with", required=False, allow_blank=True, default="")
    purposeNote = serializers.CharField(source="purpose_note", required=False, allow_blank=True, default="")
    apptStatus = serializers.ChoiceField(
        source="appt_status", choices=[c[0] for c in Visitor._meta.get_field("appt_status").choices], required=False
    )
    reviewed = serializers.BooleanField(read_only=True)
    visits = serializers.IntegerField(read_only=True)
    date = serializers.SerializerMethodField()
    requestedAt = serializers.SerializerMethodField()
    createdBy = serializers.CharField(source="created_by.name", read_only=True, default="")
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)
    reviewedBy = serializers.CharField(source="reviewed_by.name", read_only=True, default="")
    reviewedAt = serializers.DateTimeField(source="reviewed_at", read_only=True)

    class Meta:
        model = Visitor
        fields = [
            "id", "name", "phone", "cnic", "company", "meetingWith",
            "purpose", "purposeNote", "apptStatus",
            "status", "reviewed", "visits",
            "date", "requestedAt",
            "createdBy", "createdAt", "reviewedBy", "reviewedAt",
        ]
        # `status` is read-only through the normal create/update path —
        # it's only ever changed via the decide/wait/reopen actions below
        # (VisitorViewSet), which enforce can_approve_visitors themselves.
        # Kept out of validated_data entirely rather than merely
        # discouraged, so a raw PATCH can't slip a status change through.
        read_only_fields = ["status"]

    def get_date(self, obj):
        return _display_date(obj.created_at)

    def get_requestedAt(self, obj):
        return _relative_time(obj.created_at)

    def validate_name(self, value):
        if not value.strip():
            raise serializers.ValidationError("Name is required.")
        return value.strip()

    def validate_phone(self, value):
        if not value.strip():
            raise serializers.ValidationError("Phone is required.")
        return value.strip()
