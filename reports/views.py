import csv
import os
import re
from datetime import datetime, timedelta

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Count, Q
from django.db.models.functions import TruncDate
from django.http import FileResponse, StreamingHttpResponse
from django.utils import timezone
from django.utils.http import content_disposition_header
from rest_framework import permissions, status
from rest_framework.exceptions import NotFound, ParseError, PermissionDenied
from rest_framework.pagination import PageNumberPagination
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from rest_framework.views import APIView

from .catalog import build_catalog
from .constants import MAX_DAILY_FILE_MB, MAX_DAILY_FILES, MODULE_TO_CATEGORY, MODULES
from .models import ActivityLog, CustomReport, DailyReport, DailyReportFile, ReportOverride
from .permissions import ReportsStaffAccess, has_full_reports_access
from .serializers import (
    BulkDeleteSerializer,
    CatalogCreateSerializer,
    CatalogRenameSerializer,
    DailyReportInputSerializer,
    TrackSerializer,
    serialize_activity,
    serialize_daily,
    serialize_user_row,
)
from .services import log_activity, user_label
from .stats import activity_block, chart_data, currency_symbol, entity_stats, finance_stats, user_rows, workload
from .uploads import validate_upload
from .utils import RangeError, fill_days, get_tz, resolve_range

User = get_user_model()

AUTO_KEY_RE = re.compile(r"^(emp|proj|client)-\d+$|^agg-[a-z-]+$")
CSV_MAX_ROWS = 50_000


class Pagination(PageNumberPagination):
    page_size = 50
    page_size_query_param = "pageSize"
    max_page_size = 200


class ReportsAPIView(APIView):
    # IsAuthenticated first so anonymous callers still get 401; clients get 403.
    permission_classes = [permissions.IsAuthenticated, ReportsStaffAccess]

    # -- access ------------------------------------------------------------
    def is_full(self, request):
        return has_full_reports_access(request.user)

    def require_full(self, request):
        if not self.is_full(request):
            raise PermissionDenied("You don't have full access to Reports.")

    # -- parsing -----------------------------------------------------------
    def range_and_tz(self, request, default="last30"):
        tz = get_tz(request)
        try:
            return resolve_range(request.query_params, tz, default), tz
        except RangeError as exc:
            raise ParseError(str(exc))

    def handle_exception(self, exc):
        """Existing endpoints in this project answer errors as {"error": "..."}
        — keep that shape (alongside DRF's own "detail") for the frontend."""
        response = super().handle_exception(exc)
        if response is not None and isinstance(response.data, dict) and "detail" in response.data:
            response.data.setdefault("error", str(response.data["detail"]))
        return response


# ==========================================================================
# Activity log — "everything every user did"
# ==========================================================================


def activity_queryset(request, full):
    qs = ActivityLog.objects.select_related("user", "user__profile")
    if not full:
        qs = qs.filter(user=request.user)  # non-admins can only ever see their own trail
    return qs


def apply_activity_filters(request, qs, rng):
    p = request.query_params
    qs = rng.apply(qs)

    user = p.get("user")
    if user and user != "All":
        try:
            qs = qs.filter(user_id=int(user))
        except ValueError:
            raise ParseError("user must be a user id.")

    modules = [m for m in (p.get("module") or "").split(",") if m and m != "All"]
    if modules:
        qs = qs.filter(module__in=modules)

    category = p.get("category")
    if category and category != "All":
        qs = qs.filter(module__in=[m for m, c in MODULE_TO_CATEGORY.items() if c == category])

    actions = [a for a in (p.get("action") or "").split(",") if a and a != "All"]
    if actions:
        qs = qs.filter(action__in=actions)

    project = p.get("project")
    if project and project != "All":
        qs = qs.filter(project=project)

    q = (p.get("q") or "").strip()
    if q:
        qs = qs.filter(
            Q(description__icontains=q) | Q(object_repr__icontains=q)
            | Q(actor_name__icontains=q) | Q(actor_email__icontains=q)
        )
    return qs


class ActivityListView(ReportsAPIView):
    """GET /api/reports/activity/
    Filters: user, module (comma list), category, action (comma list), project,
    q, start, end | range (today|yesterday|week|month|quarter|year|last7|last30|all),
    tz, page, pageSize.  Default range = all time (newest first).
    Full-access users see everyone; everyone else sees only their own."""

    def get(self, request):
        full = self.is_full(request)
        rng, _ = self.range_and_tz(request, default="all")
        qs = apply_activity_filters(request, activity_queryset(request, full), rng)

        paginator = Pagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        response = paginator.get_paginated_response([serialize_activity(log, request) for log in page])
        response.data["scope"] = "all" if full else "own"
        return response


class ActivityFiltersView(ReportsAPIView):
    """GET /api/reports/activity/filters/ — options for the filter dropdowns."""

    def get(self, request):
        full = self.is_full(request)
        if full:
            active_ids = set(ActivityLog.objects.exclude(user__isnull=True).values_list("user_id", flat=True).distinct())
            users = User.objects.filter(Q(status="approved") | Q(id__in=active_ids)).order_by("name", "email")
        else:
            users = User.objects.filter(pk=request.user.pk)

        project_qs = ActivityLog.objects.exclude(project="")
        if not full:
            project_qs = project_qs.filter(user=request.user)

        return Response({
            "scope": "all" if full else "own",
            "users": [{"id": u.id, "name": u.name or u.email, "email": u.email, "role": u.role} for u in users],
            "modules": MODULES,
            "categories": sorted(set(MODULE_TO_CATEGORY.values())),
            "actions": [{"key": k, "label": v} for k, v in ActivityLog.ACTION_LABELS.items()],
            "projects": sorted(set(project_qs.values_list("project", flat=True).distinct()[:500])),
        })


def _csv_safe(value):
    """Stop spreadsheet formula injection: a cell starting with = + - @ would
    be executed by Excel when the export is opened."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


class _Echo:
    def write(self, value):
        return value


class ActivityExportView(ReportsAPIView):
    """GET /api/reports/activity/export/ — same filters as the list, as CSV."""

    def get(self, request):
        full = self.is_full(request)
        rng, tz = self.range_and_tz(request, default="all")
        qs = apply_activity_filters(request, activity_queryset(request, full), rng)[:CSV_MAX_ROWS]
        count = qs.count()

        log_activity(
            action="export", user=request.user, module="Reports",
            description=f"Exported activity log ({count} rows)", metadata={"rows": count},
        )

        writer = csv.writer(_Echo())
        header = ["Time", "User", "Email", "Role", "Action", "Module", "Project", "Object", "Description", "Changes", "IP"]

        def rows():
            yield "\ufeff" + writer.writerow(header)  # BOM so Excel reads UTF-8 correctly
            for log in qs.iterator(chunk_size=2000):
                changes = "; ".join(f"{k}: {v.get('from')} → {v.get('to')}" for k, v in (log.changes or {}).items())
                yield writer.writerow([_csv_safe(v) for v in [
                    log.created_at.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S"),
                    log.actor_name, log.actor_email, log.actor_role,
                    ActivityLog.ACTION_LABELS.get(log.action, log.action), log.module, log.project,
                    log.object_repr, log.description, changes, log.ip_address or "",
                ]])

        response = StreamingHttpResponse(rows(), content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="activity-{timezone.now():%Y%m%d-%H%M}.csv"'
        return response


class TrackThrottle(UserRateThrottle):
    scope = "activity_track"
    rate = "120/min"


class ActivityTrackView(ReportsAPIView):
    """POST /api/reports/activity/track/
    { module, action?, description, page?, project?, objectType?, objectRepr? }

    For things the backend can't see by itself — e.g. "opened the Tasks page",
    "searched for X". The frontend calls this on page views. Repeats of the
    same event within 20 seconds are dropped (React StrictMode double-fires)."""

    throttle_classes = [TrackThrottle]

    def post(self, request):
        ser = TrackSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data

        recent = ActivityLog.objects.filter(
            user=request.user, action=d["action"], module=d["module"], description=d["description"],
            created_at__gte=timezone.now() - timedelta(seconds=20),
        ).exists()
        if recent:
            return Response({"logged": False})

        log_activity(
            action=d["action"], user=request.user, module=d["module"], description=d["description"],
            object_type=d.get("objectType", ""), object_repr=d.get("objectRepr", ""),
            project=d.get("project", ""), metadata={"page": d["page"]} if d.get("page") else {},
        )
        return Response({"logged": True}, status=status.HTTP_201_CREATED)


# ==========================================================================
# Per-user reports — "one report per person"
# ==========================================================================


class UserReportsView(ReportsAPIView):
    """GET /api/reports/users/?range=…&q=…&role=…  (full access only)
    One row per user: totals, by module, by action, active days, last seen."""

    def get(self, request):
        self.require_full(request)
        rng, tz = self.range_and_tz(request)
        rows = user_rows(rng, tz, role=request.query_params.get("role", ""), search=(request.query_params.get("q") or "").strip())
        return Response({
            "range": {"start": rng.start, "end": rng.end},
            "count": len(rows),
            "results": [serialize_user_row(r, request) for r in rows],
        })


class UserReportDetailView(ReportsAPIView):
    """GET /api/reports/users/<id>/  or  /users/me/
    One person's full report: numbers, per-day timeline, top projects, current
    workload, their daily reports and their latest activity. Anyone can open
    their own (me); other people's need full access."""

    def get(self, request, ident):
        if ident == "me":
            target = request.user
        else:
            self.require_full(request)
            if not ident.isdigit():
                raise NotFound()
            target = User.objects.filter(pk=int(ident)).first()
            if target is None:
                raise NotFound("User not found.")

        rng, tz = self.range_and_tz(request)
        row = user_rows(rng, tz, only_ids=[target.id])[0]

        logs = rng.apply(ActivityLog.objects.filter(user=target))
        day_rows = logs.annotate(day=TruncDate("created_at", tzinfo=tz)).values("day").annotate(c=Count("id"))
        by_project = logs.exclude(project="").values("project").annotate(c=Count("id")).order_by("-c")[:10]

        recent = logs.select_related("user", "user__profile")[:50]
        dailies = (
            rng.apply_dates(DailyReport.objects.filter(user=target))
            .select_related("user", "user__profile", "approved_by").prefetch_related("files")[:10]
        )

        data = serialize_user_row(row, request)
        data.update({
            "range": {"start": rng.start, "end": rng.end},
            "timeline": fill_days({r["day"]: r["c"] for r in day_rows}, rng, tz),
            "byProject": [{"project": r["project"], "count": r["c"]} for r in by_project],
            "workload": workload(target),
            "recentActivity": [serialize_activity(log, request) for log in recent],
            "recentDailyReports": [serialize_daily(d, request) for d in dailies],
        })
        return Response(data)


# ==========================================================================
# Summary + catalog — the rest of the Reports page
# ==========================================================================


class SummaryView(ReportsAPIView):
    """GET /api/reports/summary/  (full access only) — Key Summary cards, the
    three charts, and the activity headline numbers, all from real data."""

    def get(self, request):
        self.require_full(request)
        rng, tz = self.range_and_tz(request)
        logs = rng.apply(ActivityLog.objects.all())
        dailies = DailyReport.objects.all()
        return Response({
            "range": {"start": rng.start, "end": rng.end},
            "currency": currency_symbol().strip(),
            "stats": {**entity_stats(), **finance_stats()},
            "charts": chart_data(),
            "activity": activity_block(logs, rng, tz),
            "dailyReports": {
                "total": dailies.count(),
                "pending": dailies.filter(status="pending").count(),
                "inRange": rng.apply_dates(dailies).count(),
            },
        })


class CatalogView(ReportsAPIView):
    """GET  /api/reports/catalog/   (full access) — rows of "All Reports"
    POST /api/reports/catalog/   { name, category, desc?, format?, sourceKey? } — Duplicate / Generate"""

    def get(self, request):
        self.require_full(request)
        rng, _ = self.range_and_tz(request)
        rows = build_catalog(request, rng)

        counts = {}
        for r in rows:
            counts[r["category"]] = counts.get(r["category"], 0) + 1

        category = request.query_params.get("category")
        if category and category != "All":
            rows = [r for r in rows if r["category"] == category]
        q = (request.query_params.get("q") or "").strip().lower()
        if q:
            rows = [r for r in rows if q in r["name"].lower() or q in r["by"].lower()]
        return Response({"count": len(rows), "categoryCounts": counts, "results": rows})

    def post(self, request):
        self.require_full(request)
        ser = CatalogCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        c = CustomReport.objects.create(
            name=d["name"], category=d["category"], description=d.get("desc", ""),
            format=d["format"], source_key=d.get("sourceKey", ""), created_by=request.user,
        )
        return Response({
            "id": f"custom-{c.id}", "isAuto": False, "name": c.name, "category": c.category, "desc": c.description,
            "dateRaw": c.created_at.isoformat(), "by": user_label(request.user), "avatar": None, "format": c.format,
        }, status=status.HTTP_201_CREATED)


class CatalogItemView(ReportsAPIView):
    """PATCH  /api/reports/catalog/<key>/  { name }  — rename
    DELETE /api/reports/catalog/<key>/            — delete (auto rows are hidden, custom rows removed)"""

    def _target(self, key):
        if key.startswith("custom-") and key[7:].isdigit():
            obj = CustomReport.objects.filter(pk=int(key[7:])).first()
            if obj is None:
                raise NotFound("Report not found.")
            return obj
        if AUTO_KEY_RE.match(key):
            return None  # auto-generated row
        raise NotFound("Report not found.")

    def patch(self, request, key):
        self.require_full(request)
        obj = self._target(key)
        ser = CatalogRenameSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        name = ser.validated_data["name"]
        if obj is not None:
            obj.name = name
            obj.save(update_fields=["name"])
        else:
            ReportOverride.objects.update_or_create(key=key, defaults={"custom_name": name, "updated_by": request.user})
        return Response({"id": key, "name": name})

    def delete(self, request, key):
        self.require_full(request)
        obj = self._target(key)
        if obj is not None:
            obj.delete()
        else:
            ReportOverride.objects.update_or_create(key=key, defaults={"hidden": True, "updated_by": request.user})
        return Response(status=status.HTTP_204_NO_CONTENT)


# ==========================================================================
# Daily reports (with photo/video proof)
# ==========================================================================


def daily_queryset(request, full):
    qs = DailyReport.objects.select_related("user", "user__profile", "approved_by").prefetch_related("files")
    if not full:
        qs = qs.filter(user=request.user)
    return qs


class DailyReportListCreateView(ReportsAPIView):
    """GET  /api/reports/daily/   filters: user, date, project, status, q, start/end|range, page, pageSize
    POST /api/reports/daily/   multipart: date?, note?, project?, files[] (photos/videos)"""

    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get(self, request):
        full = self.is_full(request)
        rng, _ = self.range_and_tz(request, default="all")
        p = request.query_params
        qs = rng.apply_dates(daily_queryset(request, full))

        if p.get("user") and p["user"] != "All":
            try:
                qs = qs.filter(user_id=int(p["user"]))
            except ValueError:
                raise ParseError("user must be a user id.")
        if p.get("date"):
            qs = qs.filter(date=p["date"]) if re.match(r"^\d{4}-\d{2}-\d{2}$", p["date"]) else qs.none()
        if p.get("project") and p["project"] != "All":
            qs = qs.filter(project=p["project"])
        if p.get("status") in ("pending", "approved"):
            qs = qs.filter(status=p["status"])
        q = (p.get("q") or "").strip()
        if q:
            qs = qs.filter(Q(note__icontains=q) | Q(user_name__icontains=q) | Q(project__icontains=q))

        paginator = Pagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        response = paginator.get_paginated_response([serialize_daily(r, request) for r in page])
        response.data["scope"] = "all" if full else "own"
        return response

    def post(self, request):
        user = request.user
        if user.status != "approved":
            raise PermissionDenied("Your account must be approved before you can submit daily reports.")

        ser = DailyReportInputSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data

        files = request.FILES.getlist("files") or request.FILES.getlist("files[]") or request.FILES.getlist("file")
        note = d.get("note", "")
        if not note and not files:
            return Response({"error": "Add a note or upload a photo/video first."}, status=status.HTTP_400_BAD_REQUEST)
        if len(files) > MAX_DAILY_FILES:
            return Response({"error": f"You can attach at most {MAX_DAILY_FILES} files per report."}, status=status.HTTP_400_BAD_REQUEST)

        tz = get_tz(request)
        today = datetime.now(tz).date()
        report_date = d.get("date") or today
        if report_date > today + timedelta(days=1):
            return Response({"error": "A daily report can't be dated in the future."}, status=status.HTTP_400_BAD_REQUEST)

        checked = []
        for f in files:
            try:
                kind, content_type = validate_upload(f, MAX_DAILY_FILE_MB * 1024 * 1024)
            except ValueError as exc:
                return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
            checked.append((f, kind, content_type))

        saved = []
        try:
            with transaction.atomic():
                report = DailyReport.objects.create(
                    user=user, user_name=user_label(user), user_email=user.email,
                    date=report_date, note=note, project=d.get("project", ""),
                )
                for f, kind, content_type in checked:
                    saved.append(DailyReportFile.objects.create(
                        report=report, file=f, original_name=os.path.basename(f.name)[:255],
                        content_type=content_type, size=f.size, kind=kind,
                    ))
        except Exception:
            for obj in saved:  # don't leave bytes on disk for a report that rolled back
                try:
                    obj.file.storage.delete(obj.file.name)
                except Exception:
                    pass
            raise

        log_activity(
            action="create", user=user, module="Reports",
            description=f"Submitted daily report for {report.date}" + (f" with {len(saved)} file{'' if len(saved) == 1 else 's'}" if saved else ""),
            object_type="dailyreport", object_id=str(report.id),
            object_repr=f"{report.date} — {report.project or 'No project'}", project=report.project,
            metadata={"date": str(report.date), "files": [f.original_name for f in saved]},
        )
        report = daily_queryset(request, True).get(pk=report.pk)
        return Response(serialize_daily(report, request), status=status.HTTP_201_CREATED)


class DailyReportDetailView(ReportsAPIView):
    """DELETE /api/reports/daily/<id>/ — the owner, or anyone with full access."""

    def delete(self, request, pk):
        full = self.is_full(request)
        report = daily_queryset(request, full).filter(pk=pk).first()
        if report is None:
            raise NotFound("Daily report not found.")
        report.delete()  # files are removed from disk by the post_delete handler
        return Response(status=status.HTTP_204_NO_CONTENT)


class DailyReportApproveView(ReportsAPIView):
    """POST /api/reports/daily/<id>/approve/ — full access only."""

    def post(self, request, pk):
        self.require_full(request)
        report = daily_queryset(request, True).filter(pk=pk).first()
        if report is None:
            raise NotFound("Daily report not found.")
        if report.status != "approved":
            report.status = "approved"
            report.approved_by = request.user
            report.approved_at = timezone.now()
            report.save()
        return Response(serialize_daily(report, request))


class DailyReportBulkDeleteView(ReportsAPIView):
    """POST /api/reports/daily/bulk-delete/ { ids: [..] } — powers "Delete all
    for this project". People without full access can only remove their own."""

    def post(self, request):
        ser = BulkDeleteSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        qs = daily_queryset(request, self.is_full(request)).filter(pk__in=ser.validated_data["ids"])
        deleted = 0
        for report in qs:
            report.delete()
            deleted += 1
        return Response({"deleted": deleted})


class DailyReportFileView(ReportsAPIView):
    """GET /api/reports/daily/files/<id>/ — streams a photo/video to the owner
    or a full-access user. Served through the API (not /media/) so it needs a
    valid token, unlike the rest of MEDIA_URL."""

    def get(self, request, file_id):
        f = DailyReportFile.objects.select_related("report").filter(pk=file_id).first()
        if f is None or not (self.is_full(request) or f.report.user_id == request.user.id):
            raise NotFound("File not found.")  # 404 either way, so ids can't be probed
        try:
            handle = f.file.open("rb")
        except (FileNotFoundError, ValueError):
            raise NotFound("The file is no longer on the server.")

        response = FileResponse(handle, content_type=f.content_type or "application/octet-stream")
        response["Content-Disposition"] = content_disposition_header(False, f.original_name)
        response["X-Content-Type-Options"] = "nosniff"
        response["Cache-Control"] = "private, max-age=3600"
        return response