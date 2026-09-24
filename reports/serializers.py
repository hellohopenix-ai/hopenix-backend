"""Response shapes for the Reports page.

Output is deliberately camelCase and matches the objects ReportsPage.jsx
already builds locally (daily report: userId/userName/userAvatar/files/...;
report row: id/name/category/desc/dateRaw/by/avatar/format) so the page can
swap its localStorage reads for these responses with almost no reshaping.
"""

from rest_framework import serializers

from .constants import MODULES, MODULE_TO_CATEGORY, TRACKABLE_ACTIONS
from .models import ActivityLog, CustomReport


def avatar_url(user, request):
    if user is None:
        return None
    source = user.avatar or getattr(getattr(user, "profile", None), "profile_photo", None)
    if not source:
        return None
    url = source.url
    return request.build_absolute_uri(url) if request else url


def serialize_activity(log, request):
    user = log.user
    return {
        "id": log.id,
        "userId": log.user_id,
        "userName": log.actor_name or (user.name if user else "") or "System",
        "userEmail": log.actor_email,
        "userRole": log.actor_role,
        "userAvatar": avatar_url(user, request),
        "action": log.action,
        "actionLabel": ActivityLog.ACTION_LABELS.get(log.action, log.action),
        "module": log.module,
        "category": MODULE_TO_CATEGORY.get(log.module, "General"),
        "objectType": log.object_type,
        "objectId": log.object_id,
        "objectRepr": log.object_repr,
        "description": log.description,
        "project": log.project,
        "changes": log.changes,
        "metadata": log.metadata,
        "method": log.method,
        "path": log.path,
        "ip": log.ip_address,
        "createdAt": log.created_at.isoformat(),
    }


def serialize_user_row(row, request):
    u = row["user"]
    return {
        "userId": u.id,
        "name": u.name or u.email,
        "email": u.email,
        "role": u.role,
        "department": u.department,
        "status": u.status,
        "avatar": avatar_url(u, request),
        "presence": row["presence"],
        "lastActiveAt": u.last_active_at.isoformat() if u.last_active_at else None,
        "totalActions": row["totalActions"],
        "byModule": row["byModule"],
        "byAction": row["byAction"],
        "activeDays": row["activeDays"],
        "firstActivityAt": row["firstActivityAt"].isoformat() if row["firstActivityAt"] else None,
        "lastActivityAt": row["lastActivityAt"].isoformat() if row["lastActivityAt"] else None,
        "dailyReports": row["dailyReports"],
        "dailyReportsApproved": row["dailyReportsApproved"],
    }


def serialize_daily(report, request):
    files = []
    for f in report.files.all():
        files.append({
            "id": f.id,
            "name": f.original_name,
            "type": f.content_type,
            "size": f.size,
            "kind": f.kind,
            "url": f"/api/reports/daily/files/{f.id}/",
        })
    return {
        "id": report.id,
        "userId": report.user_id,
        "userName": report.user_name or (report.user.name if report.user else "") or "Unknown",
        "userEmail": report.user_email,
        "userAvatar": avatar_url(report.user, request),
        "date": report.date.isoformat(),
        "note": report.note,
        "project": report.project,
        "status": report.status,
        "files": files,
        "createdAt": report.created_at.isoformat(),
        "approvedAt": report.approved_at.isoformat() if report.approved_at else None,
        "approvedBy": (report.approved_by.name or report.approved_by.email) if report.approved_by else None,
    }


class DailyReportInputSerializer(serializers.Serializer):
    date = serializers.DateField(required=False)
    note = serializers.CharField(required=False, allow_blank=True, max_length=5000, trim_whitespace=True)
    project = serializers.CharField(required=False, allow_blank=True, max_length=255, trim_whitespace=True)


class TrackSerializer(serializers.Serializer):
    module = serializers.ChoiceField(choices=MODULES, default="General")
    action = serializers.ChoiceField(choices=TRACKABLE_ACTIONS, default="view")
    description = serializers.CharField(max_length=300, trim_whitespace=True)
    page = serializers.CharField(max_length=255, required=False, allow_blank=True)
    project = serializers.CharField(max_length=255, required=False, allow_blank=True)
    objectType = serializers.CharField(max_length=60, required=False, allow_blank=True)
    objectRepr = serializers.CharField(max_length=255, required=False, allow_blank=True)


class CatalogCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=255, trim_whitespace=True)
    category = serializers.ChoiceField(choices=[c[0] for c in CustomReport._meta.get_field("category").choices])
    desc = serializers.CharField(required=False, allow_blank=True, max_length=2000)
    format = serializers.ChoiceField(choices=["PDF", "Excel"], default="PDF")
    sourceKey = serializers.CharField(required=False, allow_blank=True, max_length=100)


class CatalogRenameSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=255, trim_whitespace=True)


class BulkDeleteSerializer(serializers.Serializer):
    ids = serializers.ListField(child=serializers.IntegerField(), allow_empty=False, max_length=500)
