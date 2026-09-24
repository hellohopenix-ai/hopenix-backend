from django.conf import settings
from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils import timezone
from rest_framework import status, permissions
from rest_framework.authtoken.models import Token
from rest_framework.response import Response
from rest_framework.views import APIView

from .serializers import RegisterSerializer, LoginSerializer, UserSerializer, ProfileSerializer
from .throttles import (
    LoginRateThrottle,
    RegisterRateThrottle,
    OtpRateThrottle,
    PasswordResetRateThrottle,
    VerifyPasswordRateThrottle,
    GoogleAuthRateThrottle,
    EmailKeyedThrottle,
)
from reports.services import log_activity  # activity log for the Reports page (never raises)

User = get_user_model()


class IsAdmin(permissions.BasePermission):
    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.role == "admin")


class RegisterView(APIView):
    """POST /api/auth/register/  { name, email, password, company, department }
    Creates a pending 'employee' user and logs them in immediately (matches
    the old local registerUser() behaviour, which also logged in right away
    so the frontend could route to /pending-approval)."""

    permission_classes = [permissions.AllowAny]
    throttle_classes = [RegisterRateThrottle]
    throttle_scope = "register"

    def post(self, request):
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        token, _ = Token.objects.get_or_create(user=user)
        log_activity(action="register", user=user, module="Auth", description="Account registered")
        return Response(
            {"token": token.key, "user": UserSerializer(user, context={"request": request}).data},
            status=status.HTTP_201_CREATED,
        )


class LoginView(APIView):
    """POST /api/auth/login/  { email, password }
    Returns a token + user regardless of approval status - the frontend
    decides where to route (dashboard / pending-approval / rejected
    message) based on user.status, same as before."""

    permission_classes = [permissions.AllowAny]
    # IP-based AND email-based throttling together: the first stops one
    # machine from hammering /login/ at all, the second stops an attacker
    # who rotates IPs from still brute-forcing one specific account. On
    # top of both, User.failed_login_attempts/locked_until (below) gives a
    # persistent per-account lockout that survives even a cache restart.
    throttle_classes = [LoginRateThrottle, EmailKeyedThrottle]
    throttle_scope = "login"

    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"].strip().lower()
        password = serializer.validated_data["password"]

        existing_user = User.objects.filter(email=email).first()

        if existing_user and existing_user.is_locked():
            log_activity(
                action="login_blocked", module="Auth",
                description=f"Login blocked (account temporarily locked) for {email}",
                actor_email=email, metadata={"email": email}, dedupe_seconds=30,
            )
            return Response(
                {"error": "Too many failed attempts. Please try again in a few minutes."},
                status=status.HTTP_423_LOCKED,
            )

        user = authenticate(request, username=email, password=password)
        if user is None:
            if existing_user:
                existing_user.register_failed_login()
            # Same generic message whether the email doesn't exist or the
            # password is wrong, so a caller can't use this endpoint to
            # figure out which emails have accounts.
            log_activity(
                action="login_failed", module="Auth", description=f"Failed login attempt for {email}",
                actor_email=email, metadata={"email": email}, dedupe_seconds=30,
            )
            return Response({"error": "Invalid email or password."}, status=status.HTTP_401_UNAUTHORIZED)

        user.reset_failed_login()
        token, _ = Token.objects.get_or_create(user=user)
        log_activity(action="login", user=user, module="Auth", description="Logged in")
        return Response({"token": token.key, "user": UserSerializer(user, context={"request": request}).data})


class LogoutView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        log_activity(action="logout", module="Auth", description="Logged out")
        Token.objects.filter(user=request.user).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class VerifyPasswordView(APIView):
    """POST /api/auth/verify-password/  { password }
    Logged-in user apna account password re-enter karta hai (e.g. Zip
    Files page ka unlock gate) — hum kabhi bhi asal password wapas nahi
    bhejte (UserSerializer mein password field hi nahi hai), isliye
    frontend khud compare nahi kar sakta. Ye endpoint sirf True/False
    deta hai, check_password() se — jaisa Django ka login khud karta
    hai, koi plaintext kahin store/return nahi hota."""

    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [VerifyPasswordRateThrottle]
    throttle_scope = "verify_password"

    def post(self, request):
        password = request.data.get("password", "")
        return Response({"valid": request.user.check_password(password)})


class MeView(APIView):
    """GET /api/auth/me/ - restores session on refresh (frontend calls this
    on load using the saved token, instead of the old localStorage lookup)."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        request.user.touch_active()
        return Response(UserSerializer(request.user, context={"request": request}).data)


class PendingUsersView(APIView):
    permission_classes = [IsAdmin]

    def get(self, request):
        pending = User.objects.filter(status="pending")
        return Response(UserSerializer(pending, many=True, context={"request": request}).data)


class ApproveUserView(APIView):
    permission_classes = [IsAdmin]

    def post(self, request, user_id):
        try:
            target = User.objects.get(id=user_id)
        except User.DoesNotExist:
            return Response({"error": "User not found."}, status=status.HTTP_404_NOT_FOUND)
        target.status = "approved"
        role = request.data.get("role")
        department = request.data.get("department")
        if role:
            target.role = role
        if department:
            target.department = department
        target.save()
        return Response(UserSerializer(target, context={"request": request}).data)


class RejectUserView(APIView):
    permission_classes = [IsAdmin]

    def post(self, request, user_id):
        try:
            target = User.objects.get(id=user_id)
        except User.DoesNotExist:
            return Response({"error": "User not found."}, status=status.HTTP_404_NOT_FOUND)
        target.status = "rejected"
        target.save()
        return Response(UserSerializer(target, context={"request": request}).data)


class AssignManagerView(APIView):
    """POST /api/auth/users/<user_id>/assign-manager/  { "manager_id": 7 }
    Admin-only. Sets which manager this user reports to — this is what
    drives the Messages page's contact-visibility rule (a non-admin user
    only ever sees Admin + this one manager as messageable contacts, see
    messaging/permissions.py). Pass "manager_id": null to unassign."""

    permission_classes = [IsAdmin]

    def post(self, request, user_id):
        try:
            target = User.objects.get(id=user_id)
        except User.DoesNotExist:
            return Response({"error": "User not found."}, status=status.HTTP_404_NOT_FOUND)

        manager_id = request.data.get("manager_id")
        if manager_id in (None, "", "null"):
            target.manager = None
            target.save(update_fields=["manager"])
            return Response(UserSerializer(target, context={"request": request}).data)

        try:
            manager = User.objects.get(id=manager_id)
        except User.DoesNotExist:
            return Response({"error": "Manager not found."}, status=status.HTTP_404_NOT_FOUND)
        if manager.role not in ("manager", "admin"):
            return Response({"error": "Assigned manager must have the 'manager' (or 'admin') role."}, status=status.HTTP_400_BAD_REQUEST)
        if manager.id == target.id:
            return Response({"error": "A user can't be their own manager."}, status=status.HTTP_400_BAD_REQUEST)

        target.manager = manager
        target.save(update_fields=["manager"])
        return Response(UserSerializer(target, context={"request": request}).data)


class UsersListView(APIView):
    """GET /api/auth/users/ - all users (admin-only), for UserPage etc."""

    permission_classes = [IsAdmin]

    def get(self, request):
        return Response(UserSerializer(User.objects.all(), many=True, context={"request": request}).data)


class ApprovedUsersView(APIView):
    """GET /api/auth/approved-users/ - id/name/role only, for EVERY
    logged-in user (not admin-only like UsersListView above).

    FIX (client add -> auto task assigned, but the assignment message
    never reaches Messages, for a manager session): TasksPage.jsx's
    assignee-name -> real-user-id resolution (resolveRealAssignee /
    notifyAssigneesOfTaskBatch) and MessagesPage.jsx's own contact
    matching both need SOME list of real approved users to match
    against — AuthContext.jsx used to only ever populate that list via
    UsersListView above, which is admin-only, so a manager's session
    always had an empty list and every name-lookup against it silently
    failed (resolveRealAssignee -> "Unassigned", notifyAssigneesOfTaskBatch's
    targetIds stayed empty and it returned before ever calling
    apiSendMessage — no error, just nothing sent).

    UsersListView can't just be opened up to managers as-is: it returns
    the FULL UserSerializer, including salary/CNIC/bank details that
    are meant to stay admin-only. This is a separate, deliberately
    narrow serializer (id/name/role, nothing else) so any authenticated
    staff member can resolve "who is this task assigned to" / "who am I
    messaging" without exposing anyone's personal or financial data.
    Clients (role="client") are excluded — this list is for the staff
    Tasks/Messages pages, not the client portal.
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        users = User.objects.filter(status="approved").exclude(role="client").order_by("name")
        data = [{"id": u.id, "name": u.name, "role": u.role} for u in users]
        return Response(data)


# ---------------------------------------------------------------------------
# UserPage.jsx admin actions — invite / status / role / remove / salary
# ---------------------------------------------------------------------------
from django.utils.crypto import get_random_string
from django.core.mail import send_mail
from .models import Profile


class InviteUserView(APIView):
    """POST /api/auth/users/invite/  { name, email, role?, department? }
    Admin-only. Creates a brand-new pending account with a random
    temporary password and emails it to the invitee, along with a
    heads-up that they still need to log in and complete their profile
    before they'll be approved. Mirrors ctxInviteUser({name, email,
    role, department}) -> { success, error, user }."""

    permission_classes = [IsAdmin]

    def post(self, request):
        name = (request.data.get("name") or "").strip()
        email = (request.data.get("email") or "").strip().lower()
        role = (request.data.get("role") or "").strip()
        department = (request.data.get("department") or "").strip()

        if not name or not email:
            return Response({"error": "Name and email are required."}, status=status.HTTP_400_BAD_REQUEST)
        if User.objects.filter(email=email).exists():
            return Response({"error": "An account with this email already exists."}, status=status.HTTP_400_BAD_REQUEST)

        temp_password = get_random_string(12)
        user = User(
            name=name,
            email=email,
            role=role or "employee",
            department=department,
            status="pending",
        )
        user.set_password(temp_password)
        user.save()

        from_email = (
            getattr(settings, "DEFAULT_FROM_EMAIL", "")
            or getattr(settings, "EMAIL_HOST_USER", "")
            or "hello.hopenix@gmail.com"
        )
        try:
            send_mail(
                subject="You've been invited to Hopenix",
                message=(
                    f"Hi {name},\n\n"
                    f"You've been invited to join Hopenix.\n\n"
                    f"Email: {email}\n"
                    f"Temporary password: {temp_password}\n\n"
                    f"Log in and complete your profile — an admin will approve your "
                    f"account once it's submitted."
                ),
                from_email=from_email,
                recipient_list=[email],
                fail_silently=True,
            )
        except Exception as e:
            print(f"[INVITE EMAIL LOG] {email}: {e}")

        return Response({"success": True, "user": UserSerializer(user, context={"request": request}).data}, status=status.HTTP_201_CREATED)


class SetUserStatusView(APIView):
    """POST /api/auth/users/<user_id>/status/  { status }
    Admin-only. Directly sets pending/approved/rejected/deactivated —
    used for the row-level Activate/Deactivate toggle (ctxSetUserStatus)."""

    permission_classes = [IsAdmin]

    def post(self, request, user_id):
        try:
            target = User.objects.get(id=user_id)
        except User.DoesNotExist:
            return Response({"error": "User not found."}, status=status.HTTP_404_NOT_FOUND)

        new_status = request.data.get("status")
        if new_status not in dict(User.STATUS_CHOICES):
            return Response({"error": "Invalid status."}, status=status.HTTP_400_BAD_REQUEST)
        if target.role == "admin":
            return Response({"error": "Can't change an Admin account's status."}, status=status.HTTP_400_BAD_REQUEST)

        target.status = new_status
        target.save(update_fields=["status"])
        return Response(UserSerializer(target, context={"request": request}).data)


class UpdateUserRoleView(APIView):
    """POST /api/auth/users/<user_id>/update-role/  { role, department }
    Admin-only. Changes role/department on an already-approved user
    without touching their status — used by the detail modal's "Update
    Role & Department" button (ctxUpdateUserRole)."""

    permission_classes = [IsAdmin]

    def post(self, request, user_id):
        try:
            target = User.objects.get(id=user_id)
        except User.DoesNotExist:
            return Response({"error": "User not found."}, status=status.HTTP_404_NOT_FOUND)

        role = request.data.get("role")
        department = request.data.get("department")
        if not role or not department:
            return Response({"error": "role and department are required."}, status=status.HTTP_400_BAD_REQUEST)

        target.role = role
        target.department = department
        target.save(update_fields=["role", "department"])
        return Response(UserSerializer(target, context={"request": request}).data)


class RemoveUserView(APIView):
    """DELETE /api/auth/users/<user_id>/remove/
    Admin-only, permanent. Used by the row action menu's "Remove"
    (ctxRemoveUser). Admin accounts can't be removed this way."""

    permission_classes = [IsAdmin]

    def delete(self, request, user_id):
        try:
            target = User.objects.get(id=user_id)
        except User.DoesNotExist:
            return Response({"error": "User not found."}, status=status.HTTP_404_NOT_FOUND)
        if target.role == "admin":
            return Response({"error": "Can't remove an Admin account."}, status=status.HTTP_400_BAD_REQUEST)
        target.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class AdminUpdateProfileView(APIView):
    """PATCH /api/auth/users/<user_id>/profile/  { salary, bank_name, ... }
    Admin-only partial update of ANOTHER user's Profile — currently used
    for the Compensation card's salary field (handleModalSalaryUpdate ->
    updateUserProfile(id, {salary})), and reusable for admin-editable
    bank details too. Only an allow-listed set of fields can be touched
    here; everything else on Profile is self-submitted by the employee
    via CompleteProfileView."""

    permission_classes = [IsAdmin]
    ALLOWED_FIELDS = {"salary", "bank_name", "account_title", "account_number", "iban", "branch_code"}

    def patch(self, request, user_id):
        try:
            target = User.objects.get(id=user_id)
        except User.DoesNotExist:
            return Response({"error": "User not found."}, status=status.HTTP_404_NOT_FOUND)

        profile = getattr(target, "profile", None)
        if profile is None:
            return Response({"error": "This user hasn't completed their profile yet."}, status=status.HTTP_400_BAD_REQUEST)

        data = {k: v for k, v in request.data.items() if k in self.ALLOWED_FIELDS}
        if not data:
            return Response({"error": "No editable fields were provided."}, status=status.HTTP_400_BAD_REQUEST)

        serializer = ProfileSerializer(profile, data=data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(UserSerializer(target, context={"request": request}).data)


# ---------------------------------------------------------------------------
# UserPage.jsx access control — Page Access Control / Module Access
# Control / Individual User Access / AI Assistant toggle
# ---------------------------------------------------------------------------
from .models import RolePermission, ModulePermission, UserAccessOverride, UserSubPageAccess, AppSetting
from .serializers import (
    RolePermissionSerializer,
    ModulePermissionSerializer,
    UserAccessOverrideSerializer,
    UserSubPageAccessSerializer,
)


class RolePermissionsView(APIView):
    """GET /api/auth/role-permissions/
        -> { "manager": [...pages], "employee": [...], "client": [...], "accountant": [...] }
    PUT /api/auth/role-permissions/  { role, pages }
        -> upserts one role's page list.
    Backs the Page Access Control table (rolePermissions / updateRolePermissions).
    Admin is never stored here — it always has full access."""

    permission_classes = [IsAdmin]

    def get(self, request):
        data = {rp.role: rp.pages for rp in RolePermission.objects.all()}
        return Response(data)

    def put(self, request):
        role = request.data.get("role")
        pages = request.data.get("pages")
        if not role or pages is None or not isinstance(pages, list):
            return Response({"error": "role and a pages array are required."}, status=status.HTTP_400_BAD_REQUEST)
        if role == "admin":
            return Response({"error": "Admin's access can't be edited."}, status=status.HTTP_400_BAD_REQUEST)

        obj, _ = RolePermission.objects.update_or_create(role=role, defaults={"pages": pages})
        return Response({obj.role: obj.pages})


class ModulePermissionsView(APIView):
    """GET /api/auth/module-permissions/?role=employee
        -> [{role, module, view, create, edit, delete}, ...] for every module already saved for that role
    GET /api/auth/module-permissions/?role=employee&module=Tasks
        -> a single {role, module, view, create, edit, delete} object (seeded from the
           hard-coded defaults in users.access.DEFAULT_MODULE_FLAGS if never saved)
    PUT /api/auth/module-permissions/  { role, module, flag, value }
        -> sets exactly one of view/create/edit/delete.
    Backs the Module Access Control table (getModulePermissions / updateModulePermission).

    A brand-new row used to be created with every flag False (the model's
    own field defaults), which meant flipping ONE flag for a role/module
    that had never been saved before silently zeroed out the other three —
    e.g. turning on "delete" for manager/Sales would also turn OFF view/
    create/edit, even though users.access.DEFAULT_MODULE_FLAGS grants
    manager full Sales access by default. Once that row exists,
    users.access.module_flags() uses it instead of falling back to the
    defaults, so the lockout stuck. New rows are now seeded from those same
    defaults first, so saving one flag only ever changes that one flag."""

    permission_classes = [IsAdmin]
    VALID_FLAGS = {"view", "create", "edit", "delete"}

    def _get_or_seed(self, role, module):
        mp = ModulePermission.objects.filter(role=role, module=module).first()
        if mp is not None:
            return mp
        from users.access import DEFAULT_MODULE_FLAGS, role_category

        table = DEFAULT_MODULE_FLAGS.get(role_category(role), DEFAULT_MODULE_FLAGS["employee"])
        defaults = table.get(module, {"view": True, "create": False, "edit": False, "delete": False})
        return ModulePermission.objects.create(role=role, module=module, **defaults)

    def get(self, request):
        role = request.query_params.get("role")
        module = request.query_params.get("module")
        if not role:
            return Response({"error": "role is required."}, status=status.HTTP_400_BAD_REQUEST)

        if module:
            mp = self._get_or_seed(role, module)
            return Response(ModulePermissionSerializer(mp).data)

        qs = ModulePermission.objects.filter(role=role)
        return Response(ModulePermissionSerializer(qs, many=True).data)

    def put(self, request):
        role = request.data.get("role")
        module = request.data.get("module")
        flag = request.data.get("flag")
        value = request.data.get("value")
        if not role or not module or flag not in self.VALID_FLAGS:
            return Response(
                {"error": f"role, module and a flag in {sorted(self.VALID_FLAGS)} are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        mp = self._get_or_seed(role, module)
        setattr(mp, flag, bool(value))
        mp.save(update_fields=[flag])
        return Response(ModulePermissionSerializer(mp).data)


class UserAccessOverrideView(APIView):
    """GET /api/auth/users/<user_id>/access-override/
        -> {mode, pages} or null (null == "follows role default")
    PUT /api/auth/users/<user_id>/access-override/  { mode, pages? }
        -> mode "default" deletes the override (back to role default);
           "custom" requires a pages array; "full"/"none" ignore pages.
    Backs getUserAccessOverride / updateUserAccessOverride."""

    permission_classes = [IsAdmin]

    def get(self, request, user_id):
        override = UserAccessOverride.objects.filter(user_id=user_id).first()
        if not override:
            return Response(None)
        return Response(UserAccessOverrideSerializer(override).data)

    def put(self, request, user_id):
        try:
            target = User.objects.get(id=user_id)
        except User.DoesNotExist:
            return Response({"error": "User not found."}, status=status.HTTP_404_NOT_FOUND)

        mode = request.data.get("mode")
        if mode == "default":
            UserAccessOverride.objects.filter(user=target).delete()
            return Response(None)

        if mode not in dict(UserAccessOverride.MODE_CHOICES):
            return Response({"error": "mode must be default, custom, full or none."}, status=status.HTTP_400_BAD_REQUEST)

        pages = request.data.get("pages") if mode == "custom" else []
        if mode == "custom" and not isinstance(pages, list):
            return Response({"error": "pages must be an array in custom mode."}, status=status.HTTP_400_BAD_REQUEST)

        override, _ = UserAccessOverride.objects.update_or_create(
            user=target, defaults={"mode": mode, "pages": pages or []}
        )
        return Response(UserAccessOverrideSerializer(override).data)


class UserSubPageAccessView(APIView):
    """GET /api/auth/users/<user_id>/sub-access/<page>/  -> {page, mode}  (defaults to "default")
    PUT /api/auth/users/<user_id>/sub-access/<page>/  { mode }
    Backs getSubPageAccess / updateSubPageAccess for the Settings/Reports/
    Meetings "Default vs Full X Access" toggles inside the Manage Access modal."""

    permission_classes = [IsAdmin]

    def get(self, request, user_id, page):
        row = UserSubPageAccess.objects.filter(user_id=user_id, page=page).first()
        return Response({"page": page, "mode": row.mode if row else "default"})

    def put(self, request, user_id, page):
        try:
            target = User.objects.get(id=user_id)
        except User.DoesNotExist:
            return Response({"error": "User not found."}, status=status.HTTP_404_NOT_FOUND)

        mode = request.data.get("mode", "default")
        if mode not in ("default", "full"):
            return Response({"error": "mode must be default or full."}, status=status.HTTP_400_BAD_REQUEST)

        if mode == "default":
            UserSubPageAccess.objects.filter(user=target, page=page).delete()
            return Response({"page": page, "mode": "default"})

        row, _ = UserSubPageAccess.objects.update_or_create(user=target, page=page, defaults={"mode": mode})
        return Response(UserSubPageAccessSerializer(row).data)


class AiAssistantSettingView(APIView):
    """GET /api/auth/settings/ai-assistant/  -> {enabled}
    PUT /api/auth/settings/ai-assistant/  { enabled }
    Backs the global AI Assistant on/off switch (aiAssistantEnabled /
    setAiAssistantEnabled). GET is available to any authenticated user
    (the frontend needs to know the current value everywhere); only an
    admin can flip it."""

    def get_permissions(self):
        if self.request.method == "PUT":
            return [IsAdmin()]
        return [permissions.IsAuthenticated()]

    KEY = "ai_assistant_enabled"

    def get(self, request):
        setting, _ = AppSetting.objects.get_or_create(key=self.KEY, defaults={"value": False})
        return Response({"enabled": setting.value})

    def put(self, request):
        enabled = bool(request.data.get("enabled"))
        setting, _ = AppSetting.objects.update_or_create(key=self.KEY, defaults={"value": enabled})
        return Response({"enabled": setting.value})


# ---------------------------------------------------------------------------
# Email OTP verification (real Gmail SMTP send)
# ---------------------------------------------------------------------------
import random
from .models import OtpCode


class SendOtpView(APIView):
    """POST /api/auth/send-otp/  { email }
    Generates a fresh 6-digit code, emails it via Gmail SMTP, and
    invalidates any earlier unused codes for that email."""

    permission_classes = [permissions.AllowAny]
    throttle_classes = [OtpRateThrottle, EmailKeyedThrottle]
    throttle_scope = "otp"

    def post(self, request):
        email = (request.data.get("email") or "").strip().lower()
        if not email:
            return Response({"error": "Email is required."}, status=status.HTTP_400_BAD_REQUEST)

        OtpCode.objects.filter(email=email, is_used=False).update(is_used=True)  # invalidate old codes
        code = f"{random.randint(0, 999999):06d}"
        OtpCode.objects.create(email=email, code=code)

        from_email = (
            getattr(settings, "DEFAULT_FROM_EMAIL", "")
            or getattr(settings, "EMAIL_HOST_USER", "")
            or "hello.hopenix@gmail.com"
        )
        if not from_email or not from_email.strip():
            from_email = "hello.hopenix@gmail.com"

        try:
            send_mail(
                subject="Your Hopenix verification code",
                message=f"Your Hopenix verification code is: {code}\n\nThis code expires in 10 minutes.",
                from_email=from_email,
                recipient_list=[email],
                fail_silently=False,
            )
        except Exception as e:
            print(f"[OTP SEND LOG] Code for {email}: {code} | Error: {e}")
            return Response(
                {"error": f"Could not send email: {e}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        return Response({"success": True})


class VerifyOtpView(APIView):
    """POST /api/auth/verify-otp/  { email, code }"""

    permission_classes = [permissions.AllowAny]
    throttle_classes = [OtpRateThrottle, EmailKeyedThrottle]
    throttle_scope = "otp"

    def post(self, request):
        email = (request.data.get("email") or "").strip().lower()
        code = (request.data.get("code") or "").strip()

        # Look up the latest unused code for this email FIRST (regardless
        # of whether it matches `code`) so a wrong guess can be counted
        # against it — a plain filter(code=code) would silently miss the
        # row on every wrong guess and let someone try forever.
        otp = OtpCode.objects.filter(email=email, is_used=False).order_by("-created_at").first()
        if not otp:
            return Response({"error": "Incorrect code. Please check your email and try again."}, status=status.HTTP_400_BAD_REQUEST)
        if otp.is_expired():
            return Response({"error": "This code has expired. Please request a new one."}, status=status.HTTP_400_BAD_REQUEST)
        if otp.is_locked():
            otp.is_used = True
            otp.save(update_fields=["is_used"])
            return Response({"error": "Too many incorrect attempts. Please request a new code."}, status=status.HTTP_400_BAD_REQUEST)

        if otp.code != code:
            otp.register_failed_attempt()
            return Response({"error": "Incorrect code. Please check your email and try again."}, status=status.HTTP_400_BAD_REQUEST)

        otp.is_used = True
        otp.save(update_fields=["is_used"])
        return Response({"success": True})


# ---------------------------------------------------------------------------
# Google login (simple token approach, no django-allauth/JWT)
# ---------------------------------------------------------------------------
import requests as google_requests


class GoogleLoginView(APIView):
    """POST /api/auth/google-login/  { access_token }
    Used by the LOGIN page's "Continue with Google" button. Only logs
    an EXISTING account in - does not create a new one. If no account
    exists with that email, tells them to sign up instead (see
    GoogleSignupView below, used by the signup page's Google button)."""

    permission_classes = [permissions.AllowAny]
    throttle_classes = [GoogleAuthRateThrottle]
    throttle_scope = "google_auth"

    def post(self, request):
        access_token = request.data.get("access_token")
        if not access_token:
            return Response({"error": "access_token is required."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            resp = google_requests.get(
                "https://www.googleapis.com/oauth2/v3/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=5,
            )
        except google_requests.RequestException:
            return Response({"error": "Could not reach Google. Please try again."}, status=status.HTTP_502_BAD_GATEWAY)
        if resp.status_code != 200:
            return Response({"error": "Invalid or expired Google token."}, status=status.HTTP_401_UNAUTHORIZED)

        data = resp.json()
        email = (data.get("email") or "").strip().lower()
        if not email:
            return Response({"error": "Could not read an email from this Google account."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            return Response(
                {"error": "No account exists for this email. Please sign up first."},
                status=status.HTTP_404_NOT_FOUND,
            )

        token, _ = Token.objects.get_or_create(user=user)
        log_activity(action="login", user=user, module="Auth", description="Logged in with Google")
        return Response({"token": token.key, "user": UserSerializer(user, context={"request": request}).data})


class GoogleSignupView(APIView):
    """POST /api/auth/google-signup/  { access_token }
    Used by the SIGNUP page's "Continue with Google" button. Creates a
    new pending/employee account straight from the Google profile (no
    password - Google is their only login method) and logs them in, so
    the frontend can send them straight to /complete-profile. If an
    account with that email already exists, just logs them into it
    instead of erroring (a reasonable fallback if someone clicks
    "Continue with Google" on signup for an account they already have)."""

    permission_classes = [permissions.AllowAny]
    throttle_classes = [GoogleAuthRateThrottle]
    throttle_scope = "google_auth"

    def post(self, request):
        access_token = request.data.get("access_token")
        if not access_token:
            return Response({"error": "access_token is required."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            resp = google_requests.get(
                "https://www.googleapis.com/oauth2/v3/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=5,
            )
        except google_requests.RequestException:
            return Response({"error": "Could not reach Google. Please try again."}, status=status.HTTP_502_BAD_GATEWAY)
        if resp.status_code != 200:
            return Response({"error": "Invalid or expired Google token."}, status=status.HTTP_401_UNAUTHORIZED)

        data = resp.json()
        email = (data.get("email") or "").strip().lower()
        if not email:
            return Response({"error": "Could not read an email from this Google account."}, status=status.HTTP_400_BAD_REQUEST)
        if data.get("email_verified") is False:
            return Response({"error": "This Google email is not verified."}, status=status.HTTP_400_BAD_REQUEST)

        user, created = User.objects.get_or_create(
            email=email,
            defaults={"name": data.get("name", ""), "role": "employee", "status": "pending"},
        )
        if created:
            user.set_unusable_password()
            user.save()

        token, _ = Token.objects.get_or_create(user=user)
        if not created:
            log_activity(action="login", user=user, module="Auth", description="Logged in with Google")
        return Response({"token": token.key, "user": UserSerializer(user, context={"request": request}).data, "created": created})


# ---------------------------------------------------------------------------
# Forgot password (reuses the same OtpCode model/flow as login OTP)
# ---------------------------------------------------------------------------


class ResetPasswordView(APIView):
    """POST /api/auth/reset-password/  { email, code, new_password }
    Verifies the 6-digit code sent via /send-otp/ (same OtpCode model
    used for login verification) and, if valid, sets a new password for
    that account. One request does both the code check and the reset,
    matching the frontend's single "code + new password" form."""

    permission_classes = [permissions.AllowAny]
    throttle_classes = [PasswordResetRateThrottle, EmailKeyedThrottle]
    throttle_scope = "password_reset"

    def post(self, request):
        email = (request.data.get("email") or "").strip().lower()
        code = (request.data.get("code") or "").strip()
        new_password = request.data.get("new_password") or ""

        if not email or not code or not new_password:
            return Response(
                {"error": "Email, code and new password are all required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            # Same generic error as "wrong code" below — doesn't reveal
            # whether this email has an account.
            return Response(
                {"error": "Incorrect code. Please check your email and try again."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            validate_password(new_password, user=user)
        except DjangoValidationError as exc:
            return Response({"error": " ".join(exc.messages)}, status=status.HTTP_400_BAD_REQUEST)

        # Same "find latest, then compare" pattern as VerifyOtpView so a
        # wrong guess actually counts against the attempt limit instead of
        # just missing the row.
        otp = OtpCode.objects.filter(email=email, is_used=False).order_by("-created_at").first()
        if not otp:
            return Response(
                {"error": "Incorrect code. Please check your email and try again."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if otp.is_expired():
            return Response(
                {"error": "This code has expired. Please request a new one."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if otp.is_locked():
            otp.is_used = True
            otp.save(update_fields=["is_used"])
            return Response(
                {"error": "Too many incorrect attempts. Please request a new code."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if otp.code != code:
            otp.register_failed_attempt()
            return Response(
                {"error": "Incorrect code. Please check your email and try again."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        otp.is_used = True
        otp.save(update_fields=["is_used"])
        user.set_password(new_password)
        user.reset_failed_login()  # a successful reset also clears any existing lockout
        user.save()
        log_activity(action="password_reset", user=user, module="Auth", description="Password reset via email code")

        return Response({"success": True})


# ---------------------------------------------------------------------------
# Contact page (sends the message straight to the Hopenix inbox)
# ---------------------------------------------------------------------------
from django.core.mail import EmailMessage


class ContactMessageView(APIView):
    """POST /api/auth/contact/  { name, email, subject, message }
    Emails the message to the same Gmail inbox used for OTP sending
    (EMAIL_HOST_USER / DEFAULT_FROM_EMAIL), with Reply-To set to the
    sender so replying from the inbox goes straight back to them."""

    permission_classes = [permissions.AllowAny]

    def post(self, request):
        name = (request.data.get("name") or "").strip()
        email = (request.data.get("email") or "").strip()
        subject = (request.data.get("subject") or "General Inquiry").strip()
        message = (request.data.get("message") or "").strip()

        if not name or not email or not message:
            return Response(
                {"error": "Name, email and message are all required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        admin_email = getattr(settings, "DEFAULT_FROM_EMAIL", "") or getattr(settings, "EMAIL_HOST_USER", "")
        if not admin_email:
            return Response(
                {"error": "The contact inbox isn't configured yet. Please try again later."},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        body = f"From: {name} <{email}>\nSubject: {subject}\n\n{message}"

        try:
            email_msg = EmailMessage(
                subject=f"[Hopenix Contact] {subject} \u2014 {name}",
                body=body,
                from_email=admin_email,
                to=[admin_email],
                reply_to=[email],
            )
            email_msg.send(fail_silently=False)
        except Exception as e:
            return Response({"error": f"Could not send your message: {e}"}, status=status.HTTP_502_BAD_GATEWAY)

        return Response({"success": True})


# ---------------------------------------------------------------------------
# Complete Profile (personal info, address, education/experience, work
# type & skills, CV, bank details) — replaces the old localStorage-only
# updateUserProfile() flow.
# ---------------------------------------------------------------------------
import json
from rest_framework.parsers import MultiPartParser, FormParser


class CompleteProfileView(APIView):
    """POST /api/auth/complete-profile/  (multipart/form-data, authenticated)
    Fields: father_name, dob, gender, marital_status, phone, cnic,
    current_address, permanent_address, city, country, emergency_contact,
    education (JSON string), total_experience, experience (JSON string),
    work_types (JSON string), languages (JSON string),
    programming_languages (JSON string), skills (JSON string),
    cv_file (file, required on first submission),
    profile_photo (image file, required on first submission),
    id_card_front (file, optional), id_card_back (file, optional),
    bank_name, account_title, account_number, iban, branch_code.

    Creates (or updates) the logged-in user's Profile. phone and cnic are
    rejected if another account already used them. salary is NEVER
    accepted here — it's admin-only, see AdminUpdateProfileView."""

    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        raw = request.data

        def json_list(key):
            value = raw.get(key)
            if not value:
                return []
            try:
                parsed = json.loads(value)
                return parsed if isinstance(parsed, list) else []
            except (TypeError, ValueError):
                return []

        existing = Profile.objects.filter(user=request.user).first()
        cv_file = request.FILES.get("cv_file")
        photo = request.FILES.get("profile_photo")
        id_front = request.FILES.get("id_card_front")
        id_back = request.FILES.get("id_card_back")

        if not cv_file and not existing:
            return Response({"error": "A CV file is required."}, status=status.HTTP_400_BAD_REQUEST)
        if not photo and not (existing and existing.profile_photo):
            return Response({"error": "A profile photo is required."}, status=status.HTTP_400_BAD_REQUEST)

        MAX_PHOTO_MB = 5
        PHOTO_TYPES = {"image/jpeg", "image/png", "image/webp"}
        if photo:
            if photo.content_type not in PHOTO_TYPES:
                return Response(
                    {"error": "Please upload a JPG, PNG or WEBP image for the profile photo."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if photo.size > MAX_PHOTO_MB * 1024 * 1024:
                return Response(
                    {"error": f"Profile photo is too large — keep it under {MAX_PHOTO_MB}MB."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if photo.size < 1024:
                return Response(
                    {"error": "Profile photo looks empty or corrupted — please upload a valid image."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        if not (raw.get("phone") or "").strip():
            return Response({"error": "Phone number is required."}, status=status.HTTP_400_BAD_REQUEST)

        payload = {
            "father_name": raw.get("father_name", ""),
            "dob": raw.get("dob") or None,
            "gender": raw.get("gender", ""),
            "marital_status": raw.get("marital_status", ""),
            "phone": (raw.get("phone") or "").strip(),
            "cnic": (raw.get("cnic") or "").strip() or None,
            "emergency_contact": raw.get("emergency_contact", ""),
            "current_address": raw.get("current_address", ""),
            "permanent_address": raw.get("permanent_address", ""),
            "city": raw.get("city", ""),
            "country": raw.get("country", ""),
            "education": json_list("education"),
            "total_experience": raw.get("total_experience", ""),
            "experience": json_list("experience"),
            "work_types": json_list("work_types"),
            "languages": json_list("languages"),
            "programming_languages": json_list("programming_languages"),
            "skills": json_list("skills"),
            "bank_name": raw.get("bank_name", ""),
            "account_title": raw.get("account_title", ""),
            "account_number": raw.get("account_number", ""),
            "iban": raw.get("iban", ""),
            "branch_code": raw.get("branch_code", ""),
        }
        if cv_file:
            payload["cv_file"] = cv_file
        if photo:
            payload["profile_photo"] = photo
        if id_front:
            payload["id_card_front"] = id_front
        if id_back:
            payload["id_card_back"] = id_back

        serializer = ProfileSerializer(instance=existing, data=payload, partial=True)
        serializer.is_valid(raise_exception=True)
        profile = serializer.save(user=request.user)

        if not existing and profile.profile_photo:
            try:
                profile.profile_photo.open()
                photo_content = profile.profile_photo.read()
                profile.profile_photo.close()
                ext = profile.profile_photo.name.split('.')[-1] if '.' in profile.profile_photo.name else 'jpg'
                file_name = f"{uuid.uuid4()}.{ext}"
                profile.registration_photo.save(file_name, ContentFile(photo_content), save=True)
            except Exception as e:
                print(f"[REGISTRATION PHOTO COPY LOG] Error: {e}")

        return Response({"success": True, "user": UserSerializer(request.user, context={"request": request}).data})


# ---------------------------------------------------------------------------
# Lightweight self-update: avatar + phone only
# ---------------------------------------------------------------------------
import base64
import uuid
from django.core.files.base import ContentFile


class UpdateOwnProfileView(APIView):
    """PATCH /api/auth/me/update/  { name?, avatar?, phone? }  (JSON, authenticated)

    A small, fast counterpart to CompleteProfileView — for the fields a
    logged-in user routinely edits themselves from Settings, without
    resubmitting the entire "Complete Your Profile" form.

    `avatar` is a base64 data URL (e.g. "data:image/png;base64,....") —
    exactly the string format the frontend already builds from a file
    input via FileReader, so no FormData/multipart plumbing is needed on
    either side. It's decoded here and saved to User.avatar (NOT
    Profile.profile_photo — that's the one-time registration photo from
    CompleteProfilePage, kept separate so it's never silently overwritten
    here).

    `name` and `avatar` live on User directly, so they work for every
    account, including ones with no Profile at all (e.g. an admin created
    without ever going through CompleteProfilePage). Only `phone` lives
    on Profile and genuinely needs one to exist — that's the only case
    this still 400s with "please complete your profile first."."""

    permission_classes = [permissions.IsAuthenticated]

    MAX_PHOTO_MB = 5
    ALLOWED_EXTENSIONS = {"png": "png", "jpeg": "jpg", "jpg": "jpg", "webp": "webp"}

    def patch(self, request):
        user = request.user
        name = request.data.get("name")
        avatar_data_url = request.data.get("avatar")
        phone = request.data.get("phone")

        user_fields_to_save = []

        if name is not None:
            name = name.strip()
            if not name:
                return Response({"error": "Name can't be empty."}, status=status.HTTP_400_BAD_REQUEST)
            user.name = name
            user_fields_to_save.append("name")

        if avatar_data_url:
            try:
                header, b64data = avatar_data_url.split(",", 1)
                mime = header.split(":")[1].split(";")[0]  # "image/png"
                ext = self.ALLOWED_EXTENSIONS.get(mime.split("/")[-1])
            except (ValueError, IndexError):
                return Response({"error": "Invalid image data."}, status=status.HTTP_400_BAD_REQUEST)

            if not ext:
                return Response(
                    {"error": "Please upload a JPG, PNG or WEBP image."}, status=status.HTTP_400_BAD_REQUEST
                )

            try:
                raw = base64.b64decode(b64data)
            except Exception:
                return Response({"error": "Invalid image data."}, status=status.HTTP_400_BAD_REQUEST)

            if len(raw) > self.MAX_PHOTO_MB * 1024 * 1024:
                return Response(
                    {"error": f"Profile photo is too large — keep it under {self.MAX_PHOTO_MB}MB."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if len(raw) < 100:
                return Response(
                    {"error": "Profile photo looks empty or corrupted — please upload a valid image."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            user.avatar.save(f"{uuid.uuid4()}.{ext}", ContentFile(raw), save=False)
            user_fields_to_save.append("avatar")

        if user_fields_to_save:
            user.save(update_fields=user_fields_to_save)

        if phone:
            profile = getattr(user, "profile", None)
            if profile is None:
                return Response(
                    {"error": "Please complete your profile first."}, status=status.HTTP_400_BAD_REQUEST
                )
            phone = phone.strip()
            qs = Profile.objects.filter(phone=phone).exclude(pk=profile.pk)
            if qs.exists():
                return Response(
                    {"error": "This phone number is already registered to another account."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            profile.phone = phone
            profile.save(update_fields=["phone"])

        return Response(UserSerializer(user, context={"request": request}).data)


import secrets
from .models import WsTicket


class IssueWsTicketView(APIView):
    """POST /api/auth/ws-ticket/
    Called right before opening the /ws/messages/ websocket. Authenticated
    the normal way (Authorization: Token <key> header, which a plain fetch
    CAN send — unlike the WebSocket() constructor). Returns a random,
    single-use ticket valid for 30 seconds, so the long-lived auth token
    itself never has to travel in a URL query string / access log."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        ticket = WsTicket.objects.create(key=secrets.token_urlsafe(32), user=request.user)
        return Response({"ticket": ticket.key, "expires_in": 30})