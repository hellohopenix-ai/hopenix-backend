import json
import logging
import mimetypes
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import connection, transaction
from django.db.models import Sum
from django.http import FileResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import mixins, permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, ParseError, PermissionDenied, ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from dashboard.models import Income

from .models import (
    INCOME_METHOD_FOR,
    ApplicationStatus,
    CoworkingApplication,
    CoworkingPayment,
    CoworkingReview,
    CoworkingSettings,
    month_label,
    total_chairs,
)
from .permissions import IsCoworkingStaff, can_review_coworking
from .serializers import (
    ApplicationCreateSerializer,
    ApplicationRecordSerializer,
    CoworkingSettingsSerializer,
    PaymentInputSerializer,
    ReviewInputSerializer,
)

User = get_user_model()
logger = logging.getLogger(__name__)

# Arbitrary constant: the key of the Postgres advisory lock that serialises
# "approve an application" so two admins can't both take the last chairs.
_CAPACITY_LOCK_KEY = 726_015

FILE_KINDS = {"photo": "photo", "cnic-front": "cnic_front", "cnic-back": "cnic_back"}


def _notify_admins_of_application(application, request):
    """New application -> every admin gets a heads-up (they're the only ones
    who can approve), using the same "live socket if online, Web Push if not"
    split visitors/views.py uses. The payload deliberately carries no
    personal details (no CNIC, phone, address) -- just who and how many
    chairs -- since Web Push payloads leave our servers.

    Imported inside the function and wrapped in try/except so a missing or
    misconfigured messaging app can never make a submission fail; the
    application itself is already saved by the time this runs.
    """
    try:
        from messaging.consumers import is_user_online
        from messaging.push_utils import send_web_push
        from messaging.views import push_to_user
    except Exception:
        return

    who = application.company_name or application.full_name
    payload = {
        "type": "coworking.application",
        "id": application.code,
        "title": "New Coworking Application",
        "body": f"{who} applied for {application.chairs} chair{'s' if application.chairs != 1 else ''} ({application.code}).",
    }
    admins = User.objects.filter(role="admin").exclude(id=getattr(request.user, "id", None))
    for admin in admins:
        try:
            if is_user_online(admin.id):
                push_to_user(admin.id, payload)
            else:
                send_web_push(admin, payload)
        except Exception:
            logger.exception("Coworking notification to user %s failed", admin.id)


def _audit_file_view(application, kind):
    """Leave a trail on the Reports page whenever someone opens an
    applicant's ID document -- reading these is more sensitive than most
    things the request-level logger would otherwise skip (a plain GET)."""
    try:
        from reports.services import log_activity  # never raises once imported

        log_activity(
            action="view",
            module="Coworking",
            description=f"Viewed the {kind.replace('-', ' ')} of application {application.code}",
            object_type="coworkingapplication",
            object_id=str(application.pk),
            object_repr=application.code,
        )
    except Exception:
        logger.exception("Couldn't audit coworking file view")


def _read_payload(request):
    """Accept the application either as a plain JSON body, or as
    multipart/form-data with the form fields JSON-encoded in one `data` part
    plus up to three file parts (photo, cnicFront, cnicBack). The multipart
    form is what the page uses: base64-in-JSON would blow past Django's 10MB
    DATA_UPLOAD_MAX_MEMORY_SIZE with three phone-camera photos."""
    data = request.data
    if hasattr(data, "getlist"):  # QueryDict => multipart / urlencoded
        raw = data.get("data")
        if raw is None:
            raise ParseError("Send the application as JSON, or as multipart with the fields JSON-encoded in a `data` part.")
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            raise ParseError("`data` is not valid JSON.")
    else:
        payload = data
    if not isinstance(payload, dict):
        raise ParseError("Expected a JSON object.")
    payload = dict(payload)
    for name in ("photo", "cnicFront", "cnicBack"):
        upload = request.FILES.get(name)
        if upload is not None:
            payload[name] = upload
    return payload


class CoworkingApplicationViewSet(
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    """/api/coworking/applications/ -- backend for CoworkingSpacePage.jsx.

      GET    /                          list every record (newest first)
      POST   /                          submit an application (Pending)
      GET    /<code>/                   one record
      DELETE /<code>/                   admin only
      POST   /<code>/review/            admin only  -- Section J decision
      POST   /<code>/payments/          admin+manager -- tick a month's rent
      GET    /<code>/files/<kind>/      photo | cnic-front | cnic-back

    Who may do what mirrors the page's own rule ("Admin: full access.
    Manager: everything except approvals") -- see permissions.py. It is
    enforced here, not just hidden in the UI.

    <code> is the form number ("CWS-0007"), the same value the page already
    uses as a record's `id`, so no id remapping is needed on the frontend.
    """

    serializer_class = ApplicationRecordSerializer
    permission_classes = [permissions.IsAuthenticated, IsCoworkingStaff]
    parser_classes = [JSONParser, MultiPartParser, FormParser]
    lookup_field = "code"
    lookup_value_regex = r"CWS-\d+"

    def get_queryset(self):
        return CoworkingApplication.objects.select_related("created_by", "review", "review__reviewed_by").prefetch_related(
            "members", "payments"
        )

    def _record(self, code, request):
        """Fresh, fully-prefetched record -- what every write endpoint returns
        so the page can swap it straight into state."""
        app = self.get_queryset().get(code=code)
        return ApplicationRecordSerializer(app, context={"request": request}).data

    # -- submit ------------------------------------------------------------
    def create(self, request, *args, **kwargs):
        serializer = ApplicationCreateSerializer(data=_read_payload(request), context={"request": request})
        serializer.is_valid(raise_exception=True)
        application = serializer.save()
        _notify_admins_of_application(application, request)
        return Response(self._record(application.code, request), status=status.HTTP_201_CREATED)

    # -- delete ------------------------------------------------------------
    def perform_destroy(self, instance):
        if not can_review_coworking(self.request.user):
            raise PermissionDenied("Only an admin can delete an application.")
        files = [(f.storage, f.name) for f in (instance.photo, instance.cnic_front, instance.cnic_back) if f]
        with transaction.atomic():
            # Members / review / payment ticks go with it. Income rows that
            # were created from payments stay: that money was really received.
            instance.delete()

            def remove_files():
                for storage, name in files:
                    try:
                        storage.delete(name)
                    except Exception:
                        logger.exception("Couldn't remove coworking file %s", name)

            transaction.on_commit(remove_files)

    # -- Section J: office decision ----------------------------------------
    @action(detail=True, methods=["post"])
    def review(self, request, code=None):
        """Body = the ReviewPanel's `review` object. Saves the decision,
        moves the application to that status, and returns the whole record.

        Approving is refused if it would put more than TOTAL chairs in use
        (counting every other Approved application), so the "Available"
        number on the page can't go negative any more.
        """
        if not can_review_coworking(request.user):
            raise PermissionDenied("Only an admin can approve or reject an application.")
        serializer = ReviewInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        with transaction.atomic():
            if connection.vendor == "postgresql":
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_xact_lock(%s)", [_CAPACITY_LOCK_KEY])
            app = get_object_or_404(CoworkingApplication.objects.select_for_update(), code=code)

            if data["decision"] == ApplicationStatus.APPROVED:
                in_use = (
                    CoworkingApplication.objects.filter(status=ApplicationStatus.APPROVED)
                    .exclude(pk=app.pk)
                    .aggregate(n=Sum("chairs"))["n"]
                    or 0
                )
                free = max(total_chairs() - in_use, 0)
                if app.chairs > free:
                    raise ValidationError(
                        {
                            "decision": [
                                f"Not enough free chairs: {app.code} needs {app.chairs}, "
                                f"but only {free} of {total_chairs()} are free."
                            ]
                        }
                    )

            review = app.review_or_none or CoworkingReview(application=app)
            for attr, value in data.items():
                setattr(review, attr, value)
            review.reviewed_by = request.user
            review.save()

            if app.status != data["decision"]:
                app.status = data["decision"]
                app.save(update_fields=["status", "updated_at"])

        return Response(self._record(code, request))

    # -- Payment tracking: "Tick" a month --------------------------------------
    @action(detail=True, methods=["post"])
    def payments(self, request, code=None):
        """Body: { month: "2026-09", amount: 25000 }.

        Records that much rent as received for that month AND writes the
        matching row to the Income ledger (dashboard.Income) in the same
        transaction -- the page used to POST to /dashboard/incomes/ itself
        and then keep its own tally; doing both here means the two can't
        drift apart, and lets us translate "Cash"/"Card" (which Income
        would reject) to the values Income accepts.
        """
        serializer = PaymentInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        month = serializer.validated_data["month"]
        amount = serializer.validated_data["amount"]

        with transaction.atomic():
            # Lock this application's row so two quick "Tick" clicks (or two
            # people) can't each pass the "amount <= still due" check below.
            app = get_object_or_404(CoworkingApplication.objects.select_for_update(), code=code)

            if app.status != ApplicationStatus.APPROVED:
                raise ValidationError({"detail": ["Waiting for admin approval before payment can be recorded."]})
            if month not in app.billing_months():
                raise ValidationError({"month": [f"{month_label(month)} is outside this rental's billing period."]})

            paid = app.payments.filter(month=month).aggregate(total=Sum("amount"))["total"] or Decimal("0")
            remaining = app.monthly_payment - paid
            if remaining <= 0:
                raise ValidationError({"amount": [f"{month_label(month)} is already fully paid."]})
            if amount > remaining:
                raise ValidationError({"amount": [f"Only PKR {remaining:,.0f} is due for {month_label(month)}."]})

            method = serializer.validated_data.get("method") or app.payment_method
            income = Income.objects.create(
                date=timezone.localdate(),
                description=f"Coworking Space — {app.code} — {month_label(month)}",
                project="Coworking Space",
                client=(app.company_name.strip() or app.full_name)[:150],
                amount=amount,
                method=INCOME_METHOD_FOR[method],
                status="Received",
                created_by=request.user,
            )
            CoworkingPayment.objects.create(
                application=app, month=month, amount=amount, method=method, income=income, recorded_by=request.user
            )

        return Response(self._record(code, request), status=status.HTTP_201_CREATED)

    # -- ID documents (private) --------------------------------------------
    @action(
        detail=True,
        methods=["get"],
        url_path=r"files/(?P<kind>photo|cnic-front|cnic-back)",
        url_name="files",
    )
    def files(self, request, code=None, kind=None):
        """Streams one of the applicant's uploaded images. These live outside
        MEDIA_ROOT (see storage.py), so this authenticated endpoint is the
        only way to read them."""
        app = self.get_object()
        field = getattr(app, FILE_KINDS[kind])
        if not field:
            raise NotFound("No file was uploaded for this.")
        try:
            handle = field.open("rb")
        except FileNotFoundError:
            raise NotFound("The stored file is missing.")
        _audit_file_view(app, kind)
        response = FileResponse(handle, content_type=mimetypes.guess_type(field.name)[0] or "application/octet-stream")
        response["Cache-Control"] = "private, no-store"
        return response


class CoworkingSettingsView(APIView):
    """GET/PUT /api/coworking/settings/ -- the page's 4 hero stat cards
    (Total Chairs, Monthly Rate, Flexible Duration, Secure & Professional).

    Any coworking staff (admin or manager) can read it, same as the rest of
    this page; only an admin can change it, same split as review()/
    perform_destroy() above -- editing the office's chair count or rate is
    an approvals-adjacent decision, not day-to-day data entry.
    """

    permission_classes = [permissions.IsAuthenticated, IsCoworkingStaff]

    def get(self, request):
        obj = CoworkingSettings.load()
        return Response(CoworkingSettingsSerializer(obj).data)

    def put(self, request):
        if not can_review_coworking(request.user):
            raise PermissionDenied("Only an admin can update the coworking settings.")
        obj = CoworkingSettings.load()
        serializer = CoworkingSettingsSerializer(obj, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)

        # Shrinking total chairs below what's already approved would make
        # the "Available" count on the page go negative -- same guard
        # review() above uses when approving a single application.
        if "total_chairs" in serializer.validated_data:
            in_use = (
                CoworkingApplication.objects.filter(status=ApplicationStatus.APPROVED).aggregate(n=Sum("chairs"))["n"]
                or 0
            )
            new_total = serializer.validated_data["total_chairs"]
            if new_total < in_use:
                raise ValidationError(
                    {"totalChairs": [f"{in_use} chairs are already approved and in use -- can't go below that."]}
                )

        serializer.save()
        return Response(serializer.data)