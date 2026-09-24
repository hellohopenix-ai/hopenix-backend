from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone

from .managers import UserManager


def user_cv_upload_path(instance, filename):
    """media/users/<user_id>/cv/<filename> — keeps every applicant's own
    uploads (CV, photo, anything added later) grouped under one folder
    per user instead of dumping everyone's files into one shared cvs/
    directory."""
    return f"users/{instance.user_id}/cv/{filename}"


def user_photo_upload_path(instance, filename):
    """media/users/<user_id>/avatar/<filename> — same per-user grouping
    as user_cv_upload_path, for the profile photo."""
    return f"users/{instance.user_id}/avatar/{filename}"


def user_id_card_upload_path(instance, filename):
    """media/users/<user_id>/id-card/<filename> — front/back CNIC or ID
    card images shown in UserPage.jsx's detail modal (Documents section,
    idFrontUrl/idBackUrl)."""
    return f"users/{instance.user_id}/id-card/{filename}"


def user_avatar_upload_path(instance, filename):
    """media/users/<user_id>/avatar/<filename> — the account's own,
    always-editable avatar (see User.avatar below). Deliberately separate
    from user_photo_upload_path/Profile.profile_photo, which is the
    one-time registration photo submitted with the CompleteProfilePage
    form and lives on Profile, not User."""
    return f"users/{instance.id}/avatar/{filename}"


class User(AbstractUser):
    """
    Custom user model, replacing Django's default username-based User.
    Mirrors the shape the React app's AuthContext.jsx already expects:
    email (login identifier), name, company, role, department, status.
    """

    username = None  # we log in with email, not username
    email = models.EmailField(unique=True)
    name = models.CharField(max_length=255, blank=True)
    company = models.CharField(max_length=255, blank=True)
    department = models.CharField(max_length=255, blank=True)

    # A self-service display photo any account can set from Settings,
    # independent of Profile.profile_photo (which only exists once
    # CompleteProfilePage has been submitted — admins and anyone else who
    # never goes through that flow have no Profile at all, so tying the
    # editable avatar to it meant those accounts could never set one).
    # See UpdateOwnProfileView / messaging.serializers.ContactSerializer
    # and users.serializers.UserSerializer, which both check this field
    # FIRST and fall back to profile.profile_photo.
    avatar = models.ImageField(upload_to=user_avatar_upload_path, null=True, blank=True)

    ROLE_CHOICES = [
        ("admin", "Admin"),
        ("manager", "Manager"),
        ("employee", "Employee"),
        ("client", "Client"),
        ("accountant", "Accountant"),
    ]
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default="employee")

    # Who this user reports to. Drives the Messages page's contact
    # visibility rule: a non-admin user is only allowed to see/start a
    # conversation with Admin users and this one assigned manager — see
    # messaging.permissions.get_allowed_contact_ids(). Set by an admin via
    # POST /api/auth/users/<id>/assign-manager/. A manager can have their
    # own manager too (e.g. reporting to another senior manager/admin),
    # and "related_name='team_members'" is how a manager's own assigned
    # people are looked up (user.team_members.all()).
    manager = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="team_members",
        limit_choices_to={"role__in": ["manager", "admin"]},
    )

    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("approved", "Approved"),
        ("rejected", "Rejected"),
        ("deactivated", "Deactivated"),
    ]
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")

    # Cheap WhatsApp-style presence, no websockets/Channels needed: any
    # authenticated hit to MeView, the conversations list, or sending a
    # message "touches" this timestamp (see messaging/views.py). The
    # Messages page just needs an Active/Away/Offline label, which is
    # derived from how long ago this was — see presence_status() below.
    last_active_at = models.DateTimeField(null=True, blank=True)

    # --- Brute-force login protection ---------------------------------
    # Incremented on every wrong password (see LoginView). Once it hits
    # LOCKOUT_THRESHOLD the account is locked for LOCKOUT_MINUTES, even
    # if the attacker is coming from a fresh IP each time (the IP-based
    # throttle in users/throttles.py can't catch that on its own). Both
    # reset to 0/None on a successful login.
    failed_login_attempts = models.PositiveIntegerField(default=0)
    locked_until = models.DateTimeField(null=True, blank=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []  # email + password is enough at createsuperuser time

    objects = UserManager()

    LOCKOUT_THRESHOLD = 5
    LOCKOUT_MINUTES = 15

    def __str__(self):
        return self.email

    def presence_status(self):
        if not self.last_active_at:
            return "Offline"
        seconds = (timezone.now() - self.last_active_at).total_seconds()
        if seconds < 2 * 60:
            return "Active"
        if seconds < 15 * 60:
            return "Away"
        return "Offline"

    def touch_active(self):
        User.objects.filter(pk=self.pk).update(last_active_at=timezone.now())

    def is_locked(self):
        return bool(self.locked_until and self.locked_until > timezone.now())

    def register_failed_login(self):
        """Called on every wrong-password attempt for this account."""
        self.failed_login_attempts = models.F("failed_login_attempts") + 1
        self.save(update_fields=["failed_login_attempts"])
        self.refresh_from_db(fields=["failed_login_attempts"])
        if self.failed_login_attempts >= self.LOCKOUT_THRESHOLD:
            self.locked_until = timezone.now() + timezone.timedelta(minutes=self.LOCKOUT_MINUTES)
            self.save(update_fields=["locked_until"])

    def reset_failed_login(self):
        if self.failed_login_attempts or self.locked_until:
            self.failed_login_attempts = 0
            self.locked_until = None
            self.save(update_fields=["failed_login_attempts", "locked_until"])


class OtpCode(models.Model):
    """A 6-digit email verification code, real ones sent via Gmail SMTP.
    Short-lived (10 minutes) and single-use."""

    MAX_ATTEMPTS = 5  # after this many wrong guesses the code is burned

    email = models.EmailField(db_index=True)
    code = models.CharField(max_length=6)
    created_at = models.DateTimeField(auto_now_add=True)
    is_used = models.BooleanField(default=False)
    attempts = models.PositiveIntegerField(default=0)

    def is_expired(self):
        return (timezone.now() - self.created_at).total_seconds() > 10 * 60  # 10 minutes

    def is_locked(self):
        return self.attempts >= self.MAX_ATTEMPTS

    def register_failed_attempt(self):
        self.attempts = models.F("attempts") + 1
        self.save(update_fields=["attempts"])
        self.refresh_from_db(fields=["attempts"])

    def __str__(self):
        return f"{self.email} - {self.code}"


class WsTicket(models.Model):
    """A short-lived, single-use ticket that stands in for the DRF auth
    token when opening the /ws/messages/ websocket.

    Browsers' native WebSocket() can't send an Authorization header, so
    SOMETHING has to travel in the URL's query string — and query
    strings can end up in server/proxy access logs. Putting the real,
    long-lived auth token there is the risk; this ticket is a random
    value that's only good for one connection attempt within 30 seconds,
    so even if it leaked into a log, it's already useless by the time
    anyone could read it.

    Flow: frontend calls POST /api/auth/ws-ticket/ (normal Authorization:
    Token header, not the socket) right before connecting -> gets a
    ticket -> opens ws://.../ws/messages/?ticket=<this> -> messaging's
    token_auth.py middleware looks it up, checks not expired/not used,
    marks it used, and resolves it to `user`."""

    key = models.CharField(max_length=64, unique=True, db_index=True)
    user = models.ForeignKey("User", on_delete=models.CASCADE, related_name="ws_tickets")
    created_at = models.DateTimeField(auto_now_add=True)
    is_used = models.BooleanField(default=False)

    def is_expired(self):
        return (timezone.now() - self.created_at).total_seconds() > 30  # 30 seconds

    def __str__(self):
        return f"ws-ticket for {self.user.email}"


class Profile(models.Model):
    """The full "Complete Your Profile" submission (personal info, address,
    education/experience, work type & skills, CV, bank details) that used
    to live only in the frontend's localStorage. One-to-one with User —
    filled in once, right after registration, before admin approval.

    phone and cnic are unique across ALL profiles, so the same phone
    number or CNIC can't be used to complete a second account."""

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="profile")

    # Personal
    father_name = models.CharField(max_length=255, blank=True)
    dob = models.DateField(null=True, blank=True)
    gender = models.CharField(max_length=20, blank=True)
    marital_status = models.CharField(max_length=20, blank=True)
    phone = models.CharField(max_length=20, unique=True)
    cnic = models.CharField(max_length=20, unique=True, blank=True, null=True)
    emergency_contact = models.CharField(max_length=20, blank=True)

    # Address
    current_address = models.TextField(blank=True)
    permanent_address = models.TextField(blank=True)
    city = models.CharField(max_length=100, blank=True)
    country = models.CharField(max_length=100, blank=True)

    # Education & experience — stored as JSON, same shape the frontend
    # already builds (list of {degree, institute, year, grade} /
    # {company, role, duration, description}).
    education = models.JSONField(default=list, blank=True)
    total_experience = models.CharField(max_length=50, blank=True)
    experience = models.JSONField(default=list, blank=True)

    # Skills & work
    work_types = models.JSONField(default=list, blank=True)
    languages = models.JSONField(default=list, blank=True)
    programming_languages = models.JSONField(default=list, blank=True)
    skills = models.JSONField(default=list, blank=True)
    cv_file = models.FileField(upload_to=user_cv_upload_path)

    # Profile photo — compulsory, uploaded alongside the CV on
    # CompleteProfilePage.jsx. Plain FileField (not ImageField) so this
    # doesn't need Pillow added to the project just for a dimension check;
    # CompleteProfileView below already restricts it to jpg/png/webp and a
    # size range, mirroring the frontend's own validation. Exposed to the
    # frontend as UserSerializer's `avatar` field, which UserPage.jsx's
    # Avatar() component already knows how to render (it accepts any
    # http(s)/data:/blob: URL).
    profile_photo = models.FileField(upload_to=user_photo_upload_path, default="")
    registration_photo = models.FileField(upload_to=user_photo_upload_path, blank=True, null=True)

    # ID card (CNIC) front/back — shown in UserPage.jsx's detail modal
    # Documents section (idFrontUrl/idBackUrl). Optional: not every org
    # collects these at signup, so they're never required the way cv_file
    # and profile_photo are.
    id_card_front = models.FileField(upload_to=user_id_card_upload_path, blank=True, null=True)
    id_card_back = models.FileField(upload_to=user_id_card_upload_path, blank=True, null=True)

    # Bank details
    bank_name = models.CharField(max_length=255, blank=True)
    account_title = models.CharField(max_length=255, blank=True)
    account_number = models.CharField(max_length=50, blank=True)
    iban = models.CharField(max_length=34, blank=True)
    branch_code = models.CharField(max_length=50, blank=True)

    # Compensation — admin-set only (never sent by CompleteProfileView),
    # via AdminUpdateProfileView. Backs UserPage.jsx's "Compensation" card
    # (rawUser.salary / handleModalSalaryUpdate -> updateUserProfile).
    salary = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Profile of {self.user.email}"


class RolePermission(models.Model):
    """Page-level access control per role — UserPage.jsx's "Page Access
    Control" table. `pages` is the subset of ALL_PAGES (sidebar page
    names, as sent by the frontend) that this role can see. Admin is
    intentionally never stored here — it always has full access,
    enforced on the frontend (and worth re-checking server-side in any
    view that reads this)."""

    role = models.CharField(max_length=20, unique=True)
    pages = models.JSONField(default=list, blank=True)

    def __str__(self):
        return f"{self.role} -> {len(self.pages)} page(s)"


class ModulePermission(models.Model):
    """CRUD-level access control per role, per named module/page —
    UserPage.jsx's "Module Access Control" table. Field names match the
    frontend's flag keys ("view"/"create"/"edit"/"delete") exactly so no
    remapping is needed on either side."""

    role = models.CharField(max_length=20)
    module = models.CharField(max_length=100)
    view = models.BooleanField(default=False)
    create = models.BooleanField(default=False)
    edit = models.BooleanField(default=False)
    delete = models.BooleanField(default=False)

    class Meta:
        unique_together = ("role", "module")

    def __str__(self):
        return f"{self.role} / {self.module}"


class UserAccessOverride(models.Model):
    """Per-user override on top of their role's default page access —
    UserPage.jsx's "Individual User Access" / Manage Access modal.
    No row at all == "default" (falls back to RolePermission for that
    user's role); a row is only ever created for custom/full/none."""

    MODE_CHOICES = [
        ("custom", "Custom"),
        ("full", "Full Access"),
        ("none", "No Access"),
    ]
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="access_override")
    mode = models.CharField(max_length=10, choices=MODE_CHOICES)
    pages = models.JSONField(default=list, blank=True)

    def __str__(self):
        return f"{self.user.email} -> {self.mode}"


class UserSubPageAccess(models.Model):
    """Per-user, per-page "what they see once inside a page that has its
    own admin view" (Settings/Reports/Meetings tabs). mode is
    "default" (their normal, everyday view) or "full" (same as Admin,
    just for that one page)."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="sub_page_access")
    page = models.CharField(max_length=100)
    mode = models.CharField(max_length=10, default="default")

    class Meta:
        unique_together = ("user", "page")

    def __str__(self):
        return f"{self.user.email} / {self.page} -> {self.mode}"


class AppSetting(models.Model):
    """Tiny global on/off switches that used to live in a single
    component's local state — e.g. UserPage.jsx's AI Assistant toggle.
    One row per key, get_or_create'd on first read/write."""

    key = models.CharField(max_length=100, unique=True)
    value = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.key} = {self.value}"