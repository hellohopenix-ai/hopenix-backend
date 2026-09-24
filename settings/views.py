from django.contrib.auth import get_user_model
from rest_framework import permissions, status
from rest_framework.authtoken.models import Token
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import BillingInfo, CompanySettings, ExpenseSettings, IncomeSettings, NotificationPreference, ProjectSettings, SalesSettings, SecuritySetting, TaskSettings
from .serializers import (
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

# Seed list used the first time a user's Notifications tab is opened and no
# rows exist for them yet — matches SettingsPage.jsx's own DEFAULT_NOTIFICATIONS
# exactly (labels + default email/push/sms flags), so the very first load
# from the backend looks identical to what the old localStorage default did.
DEFAULT_NOTIFICATION_EVENTS = [
    {"event_key": "task_assigned", "label": "Task Assigned", "email": True, "push": True, "sms": False},
    {"event_key": "task_completed", "label": "Task Completed", "email": True, "push": False, "sms": False},
    {"event_key": "project_update", "label": "Project Update", "email": True, "push": True, "sms": False},
    {"event_key": "invoice_paid", "label": "Invoice Paid", "email": True, "push": True, "sms": True},
    {"event_key": "new_message", "label": "New Message", "email": False, "push": True, "sms": False},
    {"event_key": "system_alerts", "label": "System Alerts", "email": True, "push": True, "sms": True},
]


class IsAdminUser(permissions.BasePermission):
    """Company-wide settings (org info, billing) should only be editable
    by admins — matches the access pattern already used elsewhere in this
    project (see hasFullSubPageAccess / role checks on the frontend)."""

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and getattr(request.user, "is_staff", False))


class CompanySettingsView(APIView):
    """GET/PUT /api/settings/company/ — General tab."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        obj = CompanySettings.load()
        return Response(CompanySettingsSerializer(obj).data)

    def put(self, request):
        if not getattr(request.user, "is_staff", False):
            return Response({"error": "Only admins can update company settings."}, status=status.HTTP_403_FORBIDDEN)
        obj = CompanySettings.load()
        serializer = CompanySettingsSerializer(obj, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


class NotificationPreferencesView(APIView):
    """GET/PUT /api/settings/notifications/ — Notifications tab.
    Scoped to the logged-in user: everyone has their own notification
    preferences, unlike CompanySettings/BillingInfo which are shared."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        existing = NotificationPreference.objects.filter(user=request.user)
        if not existing.exists():
            NotificationPreference.objects.bulk_create(
                [NotificationPreference(user=request.user, **row) for row in DEFAULT_NOTIFICATION_EVENTS]
            )
            existing = NotificationPreference.objects.filter(user=request.user)
        return Response(NotificationPreferenceSerializer(existing, many=True).data)

    def put(self, request):
        # Expects the full list back, same shape GET returns:
        # [{ id, label, email, push, sms }, ...]
        rows = request.data if isinstance(request.data, list) else request.data.get("notifications", [])
        updated = []
        for row in rows:
            event_key = row.get("id") or row.get("event_key")
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
            updated.append(pref)
        return Response(NotificationPreferenceSerializer(updated, many=True).data)


class SecuritySettingView(APIView):
    """GET/PUT /api/settings/security/ — Security tab's 2FA toggle only.
    (Password change is its own endpoint below — see ChangePasswordView.)"""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        obj, _ = SecuritySetting.objects.get_or_create(user=request.user)
        return Response(SecuritySettingSerializer(obj).data)

    def put(self, request):
        obj, _ = SecuritySetting.objects.get_or_create(user=request.user)
        serializer = SecuritySettingSerializer(obj, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


class BillingInfoView(APIView):
    """GET/PUT /api/settings/billing/ — Billing tab.
    NOTE: PUT here only updates the DISPLAY record (plan name, masked
    card, etc). It does not talk to Stripe/any payment processor — wire
    that in before trusting this for anything that actually charges a
    card."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        obj = BillingInfo.load()
        return Response(BillingInfoSerializer(obj).data)

    def put(self, request):
        if not getattr(request.user, "is_staff", False):
            return Response({"error": "Only admins can update billing info."}, status=status.HTTP_403_FORBIDDEN)
        obj = BillingInfo.load()
        serializer = BillingInfoSerializer(obj, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


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
        if not getattr(request.user, "is_staff", False):
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