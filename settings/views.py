import os
import re

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import IntegrityError, transaction
from rest_framework import permissions, status
from rest_framework.authtoken.models import Token
from rest_framework.response import Response
from rest_framework.views import APIView

from . import storage_usage
from .models import BillingInfo, CompanySettings, Department, ExpenseSettings, IncomeSettings, NotificationPreference, ProjectSettings, SalesSettings, SecuritySetting, TaskSettings
from .notify import DEFAULT_NOTIFICATION_EVENTS, admin_users, deliver, is_admin
from .serializers import (
    absolute_logo_url,
    BillingInfoSerializer,
    CompanySettingsSerializer,
    ExpenseSettingsSerializer,
    IncomeSettingsSerializer,
    NotificationPreferenceSerializer,
    ProjectSettingsSerializer,
    SalesSettingsSerializer,
    SecuritySettingSerializer,
    TaskSettingsSerializer,
)

User = get_user_model()

class IsAdminUser(permissions.BasePermission):
    """Company-wide settings (org info, billing) should only be editable
    by admins — matches the access pattern already used elsewhere in this
    project (see hasFullSubPageAccess / role checks on the frontend)."""

    def has_permission(self, request, view):
        return is_admin(request.user)


class CompanySettingsView(APIView):
    """GET/PUT /api/settings/company/ — General tab."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        obj = CompanySettings.load()
        return Response(CompanySettingsSerializer(obj, context={"request": request}).data)

    def put(self, request):
        if not is_admin(request.user):
            return Response({"error": "Only admins can update company settings."}, status=status.HTTP_403_FORBIDDEN)
        obj = CompanySettings.load()
        serializer = CompanySettingsSerializer(obj, data=request.data, partial=True, context={"request": request})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


_KNOWN_EVENT_KEYS = [row["event_key"] for row in DEFAULT_NOTIFICATION_EVENTS]


def _normalize_event_key(raw):
    """Old builds of the Settings page kept notifications as ids 1..6 in
    localStorage and could PUT those back; map them to the real event keys
    instead of creating junk rows named "1", "2", ..."""
    raw = str(raw).strip()
    if raw.isdigit():
        n = int(raw)
        return _KNOWN_EVENT_KEYS[n - 1] if 1 <= n <= len(_KNOWN_EVENT_KEYS) else None
    return raw or None


class NotificationPreferencesView(APIView):
    """GET/PUT /api/settings/notifications/ — Notifications tab.
    Scoped to the logged-in user: everyone has their own notification
    preferences, unlike CompanySettings/BillingInfo which are shared.

    These toggles are now enforced: see settings/notify.py — every push /
    email the app sends for one of these events checks the recipient's row."""

    permission_classes = [permissions.IsAuthenticated]

    def _rows(self, user):
        # An older build could save rows keyed "1".."6". Never throw a user's saved
        # choices away: move such a row onto its real event key when that key has no
        # row yet, and only drop it when the real row already exists.
        have = set(NotificationPreference.objects.filter(user=user).values_list("event_key", flat=True))
        for junk in NotificationPreference.objects.filter(user=user, event_key__regex=r"^[0-9]+$"):
            real = _normalize_event_key(junk.event_key)
            if real and real not in have:
                junk.event_key = real
                junk.save(update_fields=["event_key"])
                have.add(real)
            else:
                junk.delete()
        existing = {p.event_key for p in NotificationPreference.objects.filter(user=user)}
        missing = [row for row in DEFAULT_NOTIFICATION_EVENTS if row["event_key"] not in existing]
        if missing:
            NotificationPreference.objects.bulk_create(
                [NotificationPreference(user=user, **row) for row in missing], ignore_conflicts=True
            )
        rows = list(NotificationPreference.objects.filter(user=user))
        order = {k: i for i, k in enumerate(_KNOWN_EVENT_KEYS)}
        rows.sort(key=lambda r: (order.get(r.event_key, 999), r.event_key))
        return rows

    def get(self, request):
        return Response(NotificationPreferenceSerializer(self._rows(request.user), many=True).data)

    def put(self, request):
        # Expects the full list back, same shape GET returns:
        # [{ id, label, email, push, sms }, ...]
        rows = request.data if isinstance(request.data, list) else request.data.get("notifications", [])
        for row in rows:
            if not isinstance(row, dict):
                continue
            event_key = _normalize_event_key(row.get("id") or row.get("event_key") or "")
            if not event_key:
                continue
            pref, _ = NotificationPreference.objects.get_or_create(
                user=request.user, event_key=event_key, defaults={"label": row.get("label", "")}
            )
            pref.email = bool(row.get("email", pref.email))
            pref.push = bool(row.get("push", pref.push))
            pref.sms = bool(row.get("sms", pref.sms))
            if row.get("label"):
                pref.label = row["label"]
            pref.save()
        return Response(NotificationPreferenceSerializer(self._rows(request.user), many=True).data)


class NotificationStatusView(APIView):
    """GET /api/settings/notifications/status/ — what the Notifications tab
    shows above the table: can this server push at all, and how many of MY
    devices are registered."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from messaging.push_utils import describe_push_setup

        setup = describe_push_setup(request.user)
        return Response(
            {
                "push_configured": setup["configured"],
                "push_problem": setup["reason"],
                "devices": setup["subscriptions"],
                "company_email_enabled": CompanySettings.load().email_notifications,
                "sms_configured": False,
            }
        )


class NotificationTestView(APIView):
    """POST /api/settings/notifications/test/ — sends a real notification to
    the caller right now (in-app + browser push to every device they
    registered) so they can see the whole chain works, and reports what
    actually happened instead of just saying "sent"."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        from messaging.push_utils import push_in_app, send_web_push

        payload = {
            "type": "system.alert",
            "title": "Test notification",
            "body": "Notifications are working. You can turn this on or off in Settings → Notifications.",
        }
        in_app = push_in_app(request.user, payload)
        res = send_web_push(request.user, payload, wait=True) or {}  # wait=True: real per-device result
        devices = res.get("subscriptions", 0)
        delivered = res.get("delivered", 0)
        configured = res.get("configured", False)

        if not configured:
            message = res.get("reason") or "Push isn't set up on the server."
        elif devices == 0:
            message = "No device is registered for push yet. Click “Enable on this device” first."
        elif delivered == 0:
            detail = (res.get("errors") or [{}])[0].get("detail", "")
            message = "Your device is registered but the push service rejected the message. Try re-enabling notifications." + (
                f" ({detail})" if detail else ""
            )
        else:
            message = f"Test sent to {delivered} device{'s' if delivered != 1 else ''}."
        return Response({"in_app": in_app, "devices": devices, "delivered": delivered, "configured": configured, "message": message})


class SecuritySettingView(APIView):
    """GET /api/settings/security/ — the real 2FA state (set up / disabled through /security/2fa/*).
    (Password change is its own endpoint below — see ChangePasswordView.)"""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        obj, _ = SecuritySetting.objects.get_or_create(user=request.user)
        return Response(SecuritySettingSerializer(obj).data)

    def put(self, request):
        # 2FA can't be flipped with a flag any more (see SecuritySettingSerializer);
        # kept so an older frontend that still PUTs here gets the real state back.
        obj, _ = SecuritySetting.objects.get_or_create(user=request.user)
        return Response(SecuritySettingSerializer(obj).data)


class BillingInfoView(APIView):
    """GET/PUT /api/settings/billing/ — Billing tab and the Subscription card.

    PUT (admin) accepts any of:
      plan_name                         switch plan. Price, storage allowance and
                                        renewal date come from the server-side plan
                                        catalog (plans.py), never from the request.
      card_brand, card_last4, card_expiry  the *masked* card only. The browser never
                                        sends a full card number or CVV.
    Every change is written to Billing History by the server.

    NOTE: there is still no payment processor (Stripe etc.) behind this — it
    records the subscription, it does not charge anyone."""

    permission_classes = [permissions.IsAuthenticated]

    def _data(self, obj):
        return BillingInfoSerializer(obj).data

    def get(self, request):
        return Response(self._data(BillingInfo.load()))

    def put(self, request):
        if not is_admin(request.user):
            return Response({"error": "Only admins can update billing info."}, status=status.HTTP_403_FORBIDDEN)
        from datetime import date

        from django.utils import timezone

        from . import plans
        from .models import BillingEvent

        obj = BillingInfo.load()
        data = request.data
        events = []

        new_plan = data.get("plan_name")
        if new_plan and str(new_plan).strip().lower() != (obj.plan_name or "").lower():
            plan = plans.get_plan(new_plan)
            if plan is None:
                return Response({"error": "Unknown plan."}, status=status.HTTP_400_BAD_REQUEST)
            used = storage_usage.get_usage()["used_bytes"]
            if used > plan["storage_limit_gb"] * 1024**3:
                return Response(
                    {
                        "error": f"You're using {used / 1024**3:.2f} GB, which is more than the "
                        f"{plan['name']} plan's {plan['storage_limit_gb']} GB. Free up space first."
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            obj.plan_name = plan["name"]
            obj.price = plan["price"] or 0
            obj.billing_cycle = "monthly"
            obj.storage_limit_gb = plan["storage_limit_gb"]
            obj.next_billing_date = plans.add_month(timezone.localdate())
            events.append(
                BillingEvent(
                    kind=BillingEvent.KIND_PLAN,
                    description=f"{plan['name']} Plan - Monthly" if plan["price"] else f"{plan['name']} Plan - Custom pricing",
                    amount=plan["price"] or 0,
                    created_by=request.user,
                )
            )

        if any(k in data for k in ("card_last4", "card_brand", "card_expiry")):
            last4 = str(data.get("card_last4", obj.card_last4) or "")
            brand = str(data.get("card_brand", obj.card_brand) or "").strip()
            expiry = str(data.get("card_expiry", obj.card_expiry) or "").strip()
            if not (last4.isdigit() and len(last4) == 4):
                return Response({"error": "Card number must end in 4 digits."}, status=status.HTTP_400_BAD_REQUEST)
            if brand not in plans.CARD_BRANDS:
                brand = "Card"
            if expiry:
                m = re.fullmatch(r"(0[1-9]|1[0-2])/(\d{2})", expiry)
                if not m:
                    return Response({"error": "Expiry must look like MM/YY."}, status=status.HTTP_400_BAD_REQUEST)
                today = date.today()
                if (2000 + int(m.group(2)), int(m.group(1))) < (today.year, today.month):
                    return Response({"error": "That card has expired."}, status=status.HTTP_400_BAD_REQUEST)
            if (last4, brand, expiry) != (obj.card_last4, obj.card_brand, obj.card_expiry):
                obj.card_last4, obj.card_brand, obj.card_expiry = last4, brand, expiry
                events.append(
                    BillingEvent(
                        kind=BillingEvent.KIND_CARD,
                        description=f"Payment method updated ({brand} •••• {last4})",
                        amount=0,
                        created_by=request.user,
                    )
                )

        with transaction.atomic():
            obj.save()
            for e in events:
                e.save()
        return Response(self._data(obj))


class ChangePasswordView(APIView):
    """POST /api/settings/change-password/  { current_password, new_password }
    (authenticated)

    Moved here from users/views.py so all Settings-page backend logic
    lives together. Behaviour is unchanged: request.user.check_password()
    verifies the current password against the real hashed value (the
    frontend can never see/compare a real password — UserSerializer never
    exposes one), then set_password() hashes and stores the new one. The
    old auth token is rotated (deleted + reissued) so the frontend must
    save the returned `token` and use it for further requests."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        current_password = request.data.get("current_password") or ""
        new_password = request.data.get("new_password") or ""

        if not current_password or not new_password:
            return Response(
                {"error": "Current and new password are both required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if len(new_password) < 8:
            return Response(
                {"error": "New password must be at least 8 characters."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not request.user.check_password(current_password):
            return Response({"error": "Current password is incorrect."}, status=status.HTTP_400_BAD_REQUEST)
        if request.user.check_password(new_password):
            return Response(
                {"error": "New password must be different from the current password."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        request.user.set_password(new_password)
        request.user.save(update_fields=["password"])

        Token.objects.filter(user=request.user).delete()
        token = Token.objects.create(user=request.user)

        return Response({"success": True, "token": token.key})


class _SingletonAdminSettingsView(APIView):
    """Shared GET(all)/PUT(admin-only) behaviour for the simple singleton
    settings below (Projects/Tasks/Income/Expenses/Sales) — same pattern
    as CompanySettingsView/BillingInfoView above, factored out so each
    one below is just "which model + which serializer"."""

    permission_classes = [permissions.IsAuthenticated]
    model = None
    serializer_class = None

    def get(self, request):
        obj = self.model.load()
        return Response(self.serializer_class(obj).data)

    def put(self, request):
        if not is_admin(request.user):
            return Response({"error": "Only admins can update this."}, status=status.HTTP_403_FORBIDDEN)
        obj = self.model.load()
        serializer = self.serializer_class(obj, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


class ProjectSettingsView(_SingletonAdminSettingsView):
    """GET/PUT /api/settings/projects/ — Projects tab."""

    model = ProjectSettings
    serializer_class = ProjectSettingsSerializer


class TaskSettingsView(_SingletonAdminSettingsView):
    """GET/PUT /api/settings/tasks/ — Tasks tab."""

    model = TaskSettings
    serializer_class = TaskSettingsSerializer


class IncomeSettingsView(_SingletonAdminSettingsView):
    """GET/PUT /api/settings/income/ — Income tab."""

    model = IncomeSettings
    serializer_class = IncomeSettingsSerializer


class ExpenseSettingsView(_SingletonAdminSettingsView):
    """GET/PUT /api/settings/expenses/ — Expenses tab."""

    model = ExpenseSettings
    serializer_class = ExpenseSettingsSerializer


class SalesSettingsView(_SingletonAdminSettingsView):
    """GET/PUT /api/settings/sales/ — Sales tab."""

    model = SalesSettings
    serializer_class = SalesSettingsSerializer

# ---------------------------------------------------------------------------
# Company logo — changes the logo on every page of the website
# ---------------------------------------------------------------------------
LOGO_MAX_BYTES = 2 * 1024 * 1024
LOGO_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"}
_SVG_FORBIDDEN = re.compile(rb"<\s*script|javascript:|\bon[a-z]+\s*=|<\s*foreignObject|<!ENTITY", re.IGNORECASE)


def _validate_logo(upload):
    """Returns an error message, or None if the file is an acceptable logo."""
    ext = os.path.splitext(upload.name or "")[1].lower()
    if ext not in LOGO_EXTENSIONS:
        return "Logo must be a PNG, JPG, WEBP, GIF or SVG image."
    if upload.size > LOGO_MAX_BYTES:
        return "Logo must be 2MB or smaller."
    head = upload.read()
    upload.seek(0)
    if ext == ".svg":
        text = head.lstrip()[:512].lower()
        if not (text.startswith(b"<svg") or text.startswith(b"<?xml") or b"<svg" in text):
            return "That file isn't a valid SVG image."
        if _SVG_FORBIDDEN.search(head):
            return "This SVG contains scripts, which aren't allowed. Export a plain SVG or use a PNG."
        return None
    try:
        from PIL import Image

        Image.open(upload).verify()
    except Exception:  # noqa: BLE001
        return "That file isn't a valid image."
    finally:
        upload.seek(0)
    return None


class BrandingView(APIView):
    """GET /api/settings/branding/ — PUBLIC. Just the company name + logo URL,
    nothing sensitive, so the login / register / landing pages (where nobody is
    logged in yet) can show the company's own logo too. No authentication on
    purpose: a stale token in the browser must not make this 401."""

    permission_classes = [permissions.AllowAny]
    authentication_classes = []

    def get(self, request):
        obj = CompanySettings.load()
        return Response({"name": obj.name, "logo": absolute_logo_url(obj, request), "updated_at": obj.updated_at})


class CompanyLogoView(APIView):
    """POST /api/settings/company/logo/  (multipart, field "logo")  — admin
    DELETE /api/settings/company/logo/                              — admin, back to the default"""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        if not is_admin(request.user):
            return Response({"error": "Only admins can change the company logo."}, status=status.HTTP_403_FORBIDDEN)
        upload = request.FILES.get("logo")
        if upload is None:
            return Response({"error": "Choose an image to upload."}, status=status.HTTP_400_BAD_REQUEST)
        problem = _validate_logo(upload)
        if problem:
            return Response({"error": problem}, status=status.HTTP_400_BAD_REQUEST)

        obj = CompanySettings.load()
        old = obj.logo.name if obj.logo else None
        obj.logo.save(upload.name, upload, save=False)
        obj.save()
        if old and old != obj.logo.name:
            try:
                obj.logo.storage.delete(old)
            except Exception:  # noqa: BLE001 — an undeletable old file must not fail the upload
                pass
        return Response({"logo": absolute_logo_url(obj, request), "name": obj.name})

    def delete(self, request):
        if not is_admin(request.user):
            return Response({"error": "Only admins can change the company logo."}, status=status.HTTP_403_FORBIDDEN)
        obj = CompanySettings.load()
        if obj.logo:
            old = obj.logo.name
            obj.logo = None
            obj.save()
            try:
                obj.logo.storage.delete(old)
            except Exception:  # noqa: BLE001
                pass
        return Response({"logo": "", "name": obj.name})


# ---------------------------------------------------------------------------
# Departments — the real ones, from the people actually in the system
# ---------------------------------------------------------------------------
def _dept_key(name):
    return (name or "").strip().lower()


def _sync_departments():
    """Make sure every department name that any user has is also a row in
    Department (so it can carry a head/budget and be edited). Case-insensitive:
    "design" and "Design" are the same department."""
    known = {_dept_key(d.name) for d in Department.objects.all()}
    names = {}
    for raw in User.objects.exclude(department="").values_list("department", flat=True):
        clean = " ".join((raw or "").split())
        if clean and _dept_key(clean) not in known and _dept_key(clean) not in names:
            names[_dept_key(clean)] = clean
    for clean in names.values():
        try:
            with transaction.atomic():
                Department.objects.create(name=clean)
        except IntegrityError:
            pass  # created by a parallel request


def _department_rows():
    _sync_departments()
    approved, total = {}, {}
    for dept, st, active in User.objects.exclude(department="").values_list("department", "status", "is_active"):
        k = _dept_key(dept)
        total[k] = total.get(k, 0) + 1
        if st == "approved" and active:
            approved[k] = approved.get(k, 0) + 1
    rows = []
    for d in Department.objects.select_related("head").all():
        k = _dept_key(d.name)
        rows.append(
            {
                "id": d.id,
                "name": d.name,
                "head": (d.head.name or d.head.email) if d.head else "",
                "head_id": d.head_id,
                "members": approved.get(k, 0),
                "total_users": total.get(k, 0),
                "budget": float(d.budget),
            }
        )
    return rows


def _clean_department_input(data, instance=None):
    """-> (cleaned dict, error message)"""
    out = {}
    if "name" in data or instance is None:
        name = " ".join(str(data.get("name") or "").split())
        if not name:
            return None, "Department name is required."
        if len(name) > 255:
            return None, "Department name is too long."
        clash = Department.objects.filter(name__iexact=name)
        if instance is not None:
            clash = clash.exclude(pk=instance.pk)
        if clash.exists():
            return None, f'A department named "{name}" already exists.'
        out["name"] = name
    if "head_id" in data:
        head_id = data.get("head_id")
        if head_id in (None, "", 0, "0"):
            out["head"] = None
        else:
            head = User.objects.filter(pk=head_id).first()
            if head is None:
                return None, "The selected department head doesn't exist."
            out["head"] = head
    if "budget" in data:
        try:
            budget = float(data.get("budget") or 0)
        except (TypeError, ValueError):
            return None, "Budget must be a number."
        if budget < 0 or budget > 99_999_999_999:
            return None, "Budget must be a positive number."
        out["budget"] = budget
    return out, None


class DepartmentListCreateView(APIView):
    """GET  /api/settings/departments/   any logged-in user (the Add-User form needs the list)
    POST /api/settings/departments/   admin — { name, head_id?, budget? }"""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        return Response(_department_rows())

    def post(self, request):
        if not is_admin(request.user):
            return Response({"error": "Only admins can add departments."}, status=status.HTTP_403_FORBIDDEN)
        cleaned, error = _clean_department_input(request.data)
        if error:
            return Response({"error": error}, status=status.HTTP_400_BAD_REQUEST)
        try:
            with transaction.atomic():
                dept = Department.objects.create(**cleaned)
        except IntegrityError:
            return Response({"error": "That department already exists."}, status=status.HTTP_400_BAD_REQUEST)
        row = next(r for r in _department_rows() if r["id"] == dept.id)
        return Response(row, status=status.HTTP_201_CREATED)


class DepartmentDetailView(APIView):
    """PATCH/PUT /api/settings/departments/<id>/  admin — rename / head / budget.
    Renaming also renames it on every user in that department.
    DELETE /api/settings/departments/<id>/  admin — refused while anyone is still in it."""

    permission_classes = [permissions.IsAuthenticated]

    def _get(self, pk):
        return Department.objects.filter(pk=pk).first()

    def patch(self, request, pk):
        if not is_admin(request.user):
            return Response({"error": "Only admins can edit departments."}, status=status.HTTP_403_FORBIDDEN)
        dept = self._get(pk)
        if dept is None:
            return Response({"error": "Department not found."}, status=status.HTTP_404_NOT_FOUND)
        cleaned, error = _clean_department_input(request.data, instance=dept)
        if error:
            return Response({"error": error}, status=status.HTTP_400_BAD_REQUEST)
        old_name = dept.name
        with transaction.atomic():
            for field, value in cleaned.items():
                setattr(dept, field, value)
            dept.save()
            if "name" in cleaned and cleaned["name"] != old_name:
                User.objects.filter(department__iexact=old_name).update(department=cleaned["name"])
        row = next(r for r in _department_rows() if r["id"] == dept.id)
        return Response(row)

    put = patch

    def delete(self, request, pk):
        if not is_admin(request.user):
            return Response({"error": "Only admins can delete departments."}, status=status.HTTP_403_FORBIDDEN)
        dept = self._get(pk)
        if dept is None:
            return Response({"error": "Department not found."}, status=status.HTTP_404_NOT_FOUND)
        n = User.objects.filter(department__iexact=dept.name).count()
        if n:
            return Response(
                {"error": f'"{dept.name}" still has {n} user{"s" if n != 1 else ""}. Move them to another department first.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        dept.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Real storage usage
# ---------------------------------------------------------------------------
class StorageUsageView(APIView):
    """GET /api/settings/storage/[?refresh=1]  — admin. What is really stored
    (files uploaded to the app) versus the plan's storage limit."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        if not is_admin(request.user):
            return Response({"error": "Only admins can view storage."}, status=status.HTTP_403_FORBIDDEN)
        usage = storage_usage.get_usage(force=request.query_params.get("refresh") in ("1", "true"))

        billing = BillingInfo.load()
        limit_bytes = int(float(billing.storage_limit_gb) * 1024**3)
        used = usage["used_bytes"]
        percent = round(used / limit_bytes * 100, 1) if limit_bytes else 0

        BillingInfo.objects.filter(pk=billing.pk).update(storage_used_gb=round(used / 1024**3, 2))
        self._maybe_alert_admins(percent, used, limit_bytes)

        return Response({**usage, "limit_bytes": limit_bytes, "percent": percent})

    @staticmethod
    def _maybe_alert_admins(percent, used, limit_bytes):
        """System Alerts event: tell the admins when storage passes 80%, at most
        once a day."""
        if percent < 80 or not limit_bytes:
            return
        if not cache.add("settings.storage_alert.sent", 1, 24 * 3600):
            return
        payload = {
            "type": "system.alert",
            "title": "Storage almost full",
            "body": f"Company storage is {percent:.0f}% full ({used / 1024**3:.2f} GB of {limit_bytes / 1024**3:.0f} GB).",
        }
        for admin in admin_users():
            deliver(admin, payload, "system_alerts", email_subject=payload["title"], email_body=payload["body"])


# ---------------------------------------------------------------------------
# Security: real two-factor authentication
# ---------------------------------------------------------------------------
def _password_and_code_ok(request):
    """-> (ok, error response). Needs the account password AND a valid code."""
    from . import twofactor

    password = request.data.get("password") or ""
    code = request.data.get("code") or ""
    if not request.user.check_password(password):
        request.user.register_failed_login()
        return False, Response({"error": "Password is incorrect."}, status=status.HTTP_400_BAD_REQUEST)
    if not twofactor.check_login_code(request.user, code):
        request.user.register_failed_login()
        return False, Response({"error": "That code isn't valid."}, status=status.HTTP_400_BAD_REQUEST)
    return True, None


class TwoFactorSetupView(APIView):
    """POST /api/settings/security/2fa/setup/ -> { secret, uri, qr } (qr = SVG data-URI)."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        from . import twofactor

        if twofactor.get_setting(request.user).two_factor_enabled:
            return Response({"error": "Two-factor authentication is already on."}, status=status.HTTP_400_BAD_REQUEST)
        return Response(twofactor.begin_setup(request.user))


class TwoFactorEnableView(APIView):
    """POST /api/settings/security/2fa/enable/ { code } -> { backup_codes } (shown once)."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        from . import twofactor

        codes = twofactor.confirm_setup(request.user, request.data.get("code"))
        if codes is None:
            return Response(
                {"error": "That code isn't right. Check the 6 digits in your authenticator app (and your phone's clock) and try again."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        from reports.services import log_activity

        log_activity(action="update", user=request.user, module="Settings", description="Turned on two-factor authentication")
        return Response({"two_factor_enabled": True, "backup_codes": codes})


class TwoFactorDisableView(APIView):
    """POST /api/settings/security/2fa/disable/ { password, code }"""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        from . import twofactor
        from reports.services import log_activity

        if not twofactor.get_setting(request.user).two_factor_enabled:
            return Response({"two_factor_enabled": False})
        ok, error = _password_and_code_ok(request)
        if not ok:
            return error
        twofactor.disable(request.user)
        log_activity(action="update", user=request.user, module="Settings", description="Turned off two-factor authentication")
        return Response({"two_factor_enabled": False})


class TwoFactorBackupCodesView(APIView):
    """POST /api/settings/security/2fa/backup-codes/ { password, code } — issue a fresh set."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        from . import twofactor

        sec = twofactor.get_setting(request.user)
        if not sec.two_factor_enabled:
            return Response({"error": "Turn on two-factor authentication first."}, status=status.HTTP_400_BAD_REQUEST)
        ok, error = _password_and_code_ok(request)
        if not ok:
            return error
        plain, hashes = twofactor.make_backup_codes()
        sec.refresh_from_db()
        sec.backup_codes = hashes
        sec.save(update_fields=["backup_codes", "updated_at"])
        return Response({"backup_codes": plain})


# ---------------------------------------------------------------------------
# Security: sign-in history + "sign out other devices"
# ---------------------------------------------------------------------------
def _device_label(ua):
    ua = ua or ""
    if "Edg/" in ua or "EdgA/" in ua:
        browser = "Edge"
    elif "OPR/" in ua or "Opera" in ua:
        browser = "Opera"
    elif "Firefox/" in ua or "FxiOS" in ua:
        browser = "Firefox"
    elif "Chrome/" in ua or "CriOS" in ua:
        browser = "Chrome"
    elif "Safari/" in ua:
        browser = "Safari"
    else:
        browser = "Browser"
    if "Windows" in ua:
        system = "Windows"
    elif "Android" in ua:
        system = "Android"
    elif "iPhone" in ua or "iPad" in ua or "iOS" in ua:
        system = "iPhone / iPad"
    elif "Mac OS X" in ua or "Macintosh" in ua:
        system = "macOS"
    elif "Linux" in ua:
        system = "Linux"
    else:
        system = "unknown device"
    return f"{browser} on {system}"


class SessionsView(APIView):
    """GET /api/settings/security/sessions/ — the user's REAL recent sign-ins,
    read from the activity log (device + IP + time)."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from reports.models import ActivityLog

        current_ua = (request.META.get("HTTP_USER_AGENT") or "")[:255]
        rows = ActivityLog.objects.filter(user=request.user, action="login").order_by("-created_at")[:60]
        seen, out, current_marked = set(), [], False
        for r in rows:
            key = (r.user_agent, r.ip_address)
            if key in seen:
                continue
            seen.add(key)
            is_current = (not current_marked) and r.user_agent == current_ua
            current_marked = current_marked or is_current
            out.append(
                {
                    "id": r.id,
                    "device": _device_label(r.user_agent),
                    "ip": r.ip_address or "",
                    "signed_in_at": r.created_at.isoformat(),
                    "method": "Google" if "Google" in (r.description or "") else "Password",
                    "current": is_current,
                }
            )
            if len(out) >= 8:
                break
        return Response({"sessions": out})


class SignOutOthersView(APIView):
    """POST /api/settings/security/sessions/revoke-others/ — every device signs in
    with the same token, so this issues a brand-new one: all other devices lose
    access at once and THIS device keeps working by switching to the token that
    comes back (the frontend stores it)."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        from reports.services import log_activity

        Token.objects.filter(user=request.user).delete()
        token = Token.objects.create(user=request.user)
        log_activity(action="logout", user=request.user, module="Auth", description="Signed out all other devices")
        return Response({"token": token.key})


# ---------------------------------------------------------------------------
# System logs + system information
# ---------------------------------------------------------------------------
_WARNING_ACTIONS = ("login_failed", "login_blocked", "reject")


def _log_level(row):
    code = row.status_code or 0
    if code >= 500:
        return "error"
    if row.action in _WARNING_ACTIONS or 400 <= code < 500:
        return "warning"
    return "info"


class SystemLogsView(APIView):
    """GET /api/settings/logs/?level=all|info|warning|error&limit=50 — admin. The real
    audit trail (reports.ActivityLog), not a hard-coded list."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        if not is_admin(request.user):
            return Response({"error": "Only admins can view system logs."}, status=status.HTTP_403_FORBIDDEN)
        from django.db.models import Q
        from reports.models import ActivityLog

        level = request.query_params.get("level", "all")
        try:
            limit = max(1, min(int(request.query_params.get("limit", 50)), 200))
        except ValueError:
            limit = 50
        qs = ActivityLog.objects.all()
        warning_q = Q(action__in=_WARNING_ACTIONS) | Q(status_code__gte=400, status_code__lt=500)
        error_q = Q(status_code__gte=500)
        if level == "error":
            qs = qs.filter(error_q)
        elif level == "warning":
            qs = qs.filter(warning_q).exclude(error_q)
        elif level == "info":
            qs = qs.exclude(warning_q).exclude(error_q)
        out = []
        for r in qs[:limit]:
            label = ActivityLog.ACTION_LABELS.get(r.action, r.action)
            out.append(
                {
                    "id": r.id,
                    "level": _log_level(r),
                    "message": r.description or f"{label} {r.object_repr}".strip(),
                    "time": r.created_at.isoformat(),
                    "actor": r.actor_name or r.actor_email or "System",
                    "module": r.module,
                }
            )
        return Response({"logs": out})


_STARTED_AT = None


def _started_at():
    global _STARTED_AT
    if _STARTED_AT is None:
        from django.utils import timezone

        _STARTED_AT = timezone.now()
    return _STARTED_AT


_started_at()  # remember when this server process started


class SystemInfoView(APIView):
    """GET /api/settings/system-info/ — real checks for the System Information card."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        import time as _time

        from django.conf import settings as dj
        from django.db import connection

        db_ok, db_ms = True, None
        try:
            t0 = _time.perf_counter()
            with connection.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
            db_ms = round((_time.perf_counter() - t0) * 1000)
        except Exception:  # noqa: BLE001
            db_ok = False

        cache_ok = True
        try:
            cache.set("settings.sysinfo.ping", 1, 10)
            cache_ok = cache.get("settings.sysinfo.ping") == 1
        except Exception:  # noqa: BLE001
            cache_ok = False

        sha = os.environ.get("RAILWAY_GIT_COMMIT_SHA") or os.environ.get("GIT_COMMIT") or os.environ.get("SOURCE_VERSION") or ""
        try:
            from messaging.push_utils import describe_push_setup

            push_ok = describe_push_setup(request.user)["configured"]
        except Exception:  # noqa: BLE001
            push_ok = False
        email_ok = bool(getattr(dj, "DEFAULT_FROM_EMAIL", "")) and "console" not in str(getattr(dj, "EMAIL_BACKEND", "")).lower()

        healthy = db_ok and cache_ok
        return Response(
            {
                "version": sha[:7] if sha else "",
                "started_at": _started_at().isoformat(),
                "status": "operational" if healthy else "degraded",
                "database": {"ok": db_ok, "latency_ms": db_ms, "engine": connection.vendor},
                "cache": {"ok": cache_ok},
                "storage": storage_usage.get_usage()["source"] if is_admin(request.user) else "",
                "push_configured": push_ok,
                "email_configured": email_ok,
                # There is no backup job in this project, so say so rather than invent a date.
                "backup": {"tracked": False, "note": "Backups are handled by your database host, not by this app."},
            }
        )


# ---------------------------------------------------------------------------
# Danger zone
# ---------------------------------------------------------------------------
class AccountDeletionView(APIView):
    """GET/POST/DELETE /api/settings/account/delete-request/ — admin.
    Records (and can cancel) a deletion REQUEST and alerts the other admins.
    It never deletes anything by itself."""

    permission_classes = [permissions.IsAuthenticated]

    def _open(self, user):
        from .models import AccountDeletionRequest

        return AccountDeletionRequest.objects.filter(user=user, cancelled_at__isnull=True).first()

    def _out(self, req):
        return {"requested": bool(req), "requested_at": req.requested_at.isoformat() if req else None}

    def get(self, request):
        return Response(self._out(self._open(request.user)))

    def post(self, request):
        if not is_admin(request.user):
            return Response({"error": "Only admins can request account deletion."}, status=status.HTTP_403_FORBIDDEN)
        from .models import AccountDeletionRequest

        if not request.user.check_password(request.data.get("password") or ""):
            return Response({"error": "Password is incorrect."}, status=status.HTTP_400_BAD_REQUEST)
        req = self._open(request.user) or AccountDeletionRequest.objects.create(user=request.user)
        payload = {
            "type": "system.alert",
            "title": "Account deletion requested",
            "body": f"{request.user.name or request.user.email} asked to delete the company account. Nothing has been deleted.",
        }
        for admin in admin_users(exclude_ids=[request.user.id]):
            deliver(admin, payload, "system_alerts", email_subject=payload["title"], email_body=payload["body"])
        return Response(self._out(req), status=status.HTTP_201_CREATED)

    def delete(self, request):
        from django.utils import timezone

        req = self._open(request.user)
        if req:
            req.cancelled_at = timezone.now()
            req.save(update_fields=["cancelled_at"])
        return Response(self._out(None))


class SettingsResetView(APIView):
    """POST /api/settings/reset/ — admin. Puts the preference settings back to their
    defaults: Projects / Tasks / Income / Expenses / Sales, the display options on
    General (language, currency, formats...), and YOUR notification choices.
    It does NOT touch the company name/contact/address, logo, billing, users,
    departments or anything people have created."""

    permission_classes = [permissions.IsAuthenticated]

    GENERAL_PREFERENCE_FIELDS = [
        "timezone", "currency", "date_format", "time_format", "default_dashboard",
        "language", "compact_mode", "email_notifications", "auto_currency_update",
    ]

    def post(self, request):
        if not is_admin(request.user):
            return Response({"error": "Only admins can reset settings."}, status=status.HTTP_403_FORBIDDEN)
        with transaction.atomic():
            for model in (ProjectSettings, TaskSettings, IncomeSettings, ExpenseSettings, SalesSettings):
                model.objects.all().delete()
                model.load()
            company = CompanySettings.load()
            for name in self.GENERAL_PREFERENCE_FIELDS:
                if hasattr(company, name):
                    setattr(company, name, company._meta.get_field(name).get_default())
            company.save()
            NotificationPreference.objects.filter(user=request.user).delete()
        return Response({"ok": True})


# ---------------------------------------------------------------------------
# Read-only config for the rest of the app
# ---------------------------------------------------------------------------
class AppConfigView(APIView):
    """GET /api/settings/app-config/ — any logged-in user. The saved module settings
    (categories, statuses, rules) so pages other than Settings can use them.
    Also runs the hourly auto-archive sweep (Settings -> Projects)."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from . import rules

        rules.maybe_archive()
        company = CompanySettings.load()
        return Response(
            {
                "company": {
                    "currency": company.currency, "timezone": company.timezone, "date_format": company.date_format,
                    "time_format": company.time_format, "language": company.language,
                },
                "projects": ProjectSettingsSerializer(ProjectSettings.load()).data,
                "tasks": TaskSettingsSerializer(TaskSettings.load()).data,
                "income": IncomeSettingsSerializer(IncomeSettings.load()).data,
                "expenses": ExpenseSettingsSerializer(ExpenseSettings.load()).data,
                "sales": SalesSettingsSerializer(SalesSettings.load()).data,
            }
        )
