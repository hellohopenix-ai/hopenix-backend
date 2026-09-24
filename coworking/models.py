import os
import uuid
from datetime import date
from decimal import Decimal

from django.conf import settings
from django.db import models, transaction
from django.utils import timezone

from .storage import get_private_storage

# Kept in sync by hand with CoworkingSpacePage.jsx (the frontend owns the
# source of truth for these dropdown values, same convention visitors/ uses).
APPLICANT_TYPES = ["Individual", "Freelancer", "Startup", "Company", "Agency", "Other"]
DOCUMENT_LIST = ["Address Proof", "Company Registration", "NTN", "Authority Letter"]


class CoworkingSettings(models.Model):
    """The 4 hero stat cards at the top of CoworkingSpacePage.jsx (Total
    Chairs / Monthly Rate / Flexible Duration / Secure & Professional).

    Singleton row (always pk=1) -- get_or_create'd on first access, same
    pattern as settings.CompanySettings and users.AppSetting. Editable from
    the page itself (admin only) instead of needing a code change +
    redeploy, which is what the old COWORKING_TOTAL_CHAIRS Django-settings
    override required.
    """

    total_chairs = models.PositiveSmallIntegerField(default=12)
    monthly_rate = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("25000"))
    min_duration_months = models.PositiveSmallIntegerField(default=1)
    max_duration_months = models.PositiveSmallIntegerField(default=12)
    duration_note = models.CharField(max_length=100, blank=True, default="Custom options available")
    security_features = models.CharField(max_length=150, blank=True, default="Access control, CCTV")
    support_hours = models.CharField(max_length=50, blank=True, default="24/7 Support")
    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self):
        return "Coworking Settings"


def total_chairs():
    """Total chairs the office has, used everywhere capacity is checked
    (Available count on the page, the review approval guard below). Backed
    by CoworkingSettings now so admins can change it from the page instead
    of editing settings.py; COWORKING_TOTAL_CHAIRS in settings.py is still
    honoured as a deploy-time override for anyone relying on it, but the
    database value wins once it exists."""
    return int(getattr(settings, "COWORKING_TOTAL_CHAIRS", None) or CoworkingSettings.load().total_chairs)


class ApplicationStatus(models.TextChoices):
    PENDING = "Pending", "Pending"
    APPROVED = "Approved", "Approved"
    REJECTED = "Rejected", "Rejected"


class Seating(models.TextChoices):
    DEDICATED = "Dedicated", "Dedicated"
    FLEXIBLE = "Flexible / Hot Desk", "Flexible / Hot Desk"


class AccessHours(models.TextChoices):
    OFFICE_HOURS = "Office Hours", "Office Hours"
    EXTENDED = "Extended (if approved)", "Extended (if approved)"


class PaymentMethod(models.TextChoices):
    BANK_TRANSFER = "Bank Transfer", "Bank Transfer"
    CASH = "Cash", "Cash"
    CARD = "Card", "Card"
    JAZZCASH = "JazzCash", "JazzCash"
    EASYPAISA = "Easypaisa", "Easypaisa"


# dashboard.Income only accepts Bank Transfer / JazzCash / Easypaisa /
# "Cash in Hand" (see Income.METHOD_CHOICES). The coworking form also offers
# "Cash" and "Card", which Income would reject with a 400 -- so a payment is
# translated here when it is written to the ledger. (A card payment settles
# into the bank account, hence Bank Transfer.)
INCOME_METHOD_FOR = {
    PaymentMethod.BANK_TRANSFER: "Bank Transfer",
    PaymentMethod.CASH: "Cash in Hand",
    PaymentMethod.CARD: "Bank Transfer",
    PaymentMethod.JAZZCASH: "JazzCash",
    PaymentMethod.EASYPAISA: "Easypaisa",
}


class BillingCycle(models.TextChoices):
    MONTHLY = "Monthly", "Monthly"
    QUARTERLY = "Quarterly", "Quarterly"
    BIANNUAL = "Bi-Annually", "Bi-Annually"


class ApproverRole(models.TextChoices):
    CEO = "CEO", "CEO"
    MD = "MD", "MD"
    MANAGER = "Manager", "Manager"
    PM = "PM", "PM"
    ADMIN = "Admin", "Admin"


def _upload_path(kind):
    def path(instance, filename):
        ext = os.path.splitext(filename)[1].lower()[:6]
        return f"coworking/{uuid.uuid4().hex}/{kind}{ext}"

    return path


# Named functions (not lambdas) so the migration can serialise them.
def photo_path(instance, filename):
    return _upload_path("photo")(instance, filename)


def cnic_front_path(instance, filename):
    return _upload_path("cnic-front")(instance, filename)


def cnic_back_path(instance, filename):
    return _upload_path("cnic-back")(instance, filename)


def month_label(key):
    """"2026-09" -> "September 2026" (same wording the page's own
    monthLabel() produces)."""
    year, month = (int(p) for p in key.split("-"))
    return date(year, month, 1).strftime("%B %Y")


class CoworkingApplication(models.Model):
    """One submitted CWS-01 "Coworking Space Form". Backs
    CoworkingSpacePage.jsx's `records[]` (the page's own `application`
    object, sections A-I) -- the office-use section J lives in
    CoworkingReview and the month-by-month rent ticks in CoworkingPayment.

    Money fields at the bottom (subtotal / discount_amount / grand_total /
    monthly_payment) are always recomputed here from chairs x rate x
    duration etc. The frontend also sends its own copy of those numbers, but
    they're ignored -- a client must never be the source of truth for what
    someone owes.
    """

    # Human-facing form number ("CWS-0007") -- derived from the row id right
    # after the first insert, so it is unique, race-free and never reused
    # after a delete (matters: Income rows created from payments carry it in
    # their description). NULL only for the split second before that update.
    code = models.CharField(max_length=16, unique=True, null=True, editable=False)
    status = models.CharField(
        max_length=10, choices=ApplicationStatus.choices, default=ApplicationStatus.PENDING, db_index=True
    )

    # A. Application record
    application_date = models.DateField(default=timezone.localdate)
    agreement_id = models.CharField(max_length=64, blank=True, default="")
    start_date = models.DateField(null=True, blank=True)

    # B. Applicant / occupant details
    full_name = models.CharField(max_length=255)
    father_name = models.CharField(max_length=255, blank=True, default="")
    cnic = models.CharField(max_length=32)  # CNIC or passport number
    dob = models.DateField(null=True, blank=True)
    mobile = models.CharField(max_length=32)
    whatsapp = models.CharField(max_length=32, blank=True, default="")
    email = models.EmailField(blank=True, default="")
    address = models.TextField()
    photo = models.ImageField(upload_to=photo_path, storage=get_private_storage, null=True, blank=True)
    cnic_front = models.ImageField(upload_to=cnic_front_path, storage=get_private_storage, null=True, blank=True)
    cnic_back = models.ImageField(upload_to=cnic_back_path, storage=get_private_storage, null=True, blank=True)

    # C. Work / business profile
    applicant_type = models.JSONField(default=list, blank=True)  # subset of APPLICANT_TYPES
    company_name = models.CharField(max_length=255, blank=True, default="")
    legal_status = models.CharField(max_length=100, blank=True, default="")
    registration_no = models.CharField(max_length=100, blank=True, default="")
    nature_of_work = models.CharField(max_length=255, blank=True, default="")
    website = models.CharField(max_length=255, blank=True, default="")
    team_size = models.PositiveIntegerField(null=True, blank=True)

    # D. Workspace requirements
    chairs = models.PositiveSmallIntegerField()
    rate_per_chair = models.DecimalField(max_digits=10, decimal_places=2)
    duration = models.PositiveSmallIntegerField(help_text="Months")
    expected_end_date = models.DateField(null=True, blank=True)
    seating = models.CharField(max_length=30, choices=Seating.choices, default=Seating.DEDICATED)
    access = models.CharField(max_length=30, choices=AccessHours.choices, default=AccessHours.OFFICE_HOURS)
    assigned_chairs = models.CharField(max_length=100, blank=True, default="")
    special_notes = models.TextField(blank=True, default="")

    # G. Documents & emergency contact
    documents = models.JSONField(default=list, blank=True)  # subset of DOCUMENT_LIST
    emergency_name = models.CharField(max_length=255)
    emergency_relation = models.CharField(max_length=100, blank=True, default="")
    emergency_phone = models.CharField(max_length=32)
    emergency_alt_phone = models.CharField(max_length=32, blank=True, default="")

    # F. Monthly rent & payment summary (inputs)
    discount_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    security_deposit = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    other_charges = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    payment_method = models.CharField(max_length=20, choices=PaymentMethod.choices, default=PaymentMethod.BANK_TRANSFER)
    transaction_no = models.CharField(max_length=100, blank=True, default="")
    billing_due_day = models.CharField(max_length=20, blank=True, default="5th")
    billing_cycle = models.CharField(max_length=20, choices=BillingCycle.choices, default=BillingCycle.MONTHLY)

    # F. ...and the figures derived from them (see recalculate_totals)
    monthly_payment = models.DecimalField(max_digits=15, decimal_places=2, default=0, editable=False)
    subtotal = models.DecimalField(max_digits=15, decimal_places=2, default=0, editable=False)
    discount_amount = models.DecimalField(max_digits=15, decimal_places=2, default=0, editable=False)
    grand_total = models.DecimalField(max_digits=15, decimal_places=2, default=0, editable=False)

    # H / I. Terms & declaration
    terms_agreed = models.BooleanField(default=False)
    declaration_name = models.CharField(max_length=255, blank=True, default="")
    declaration_date = models.DateField(default=timezone.localdate)

    # Financial records should outlive the staff account that entered them,
    # so a deleted user just blanks this (same as dashboard.Income).
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="coworking_applications"
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.code or 'new'} — {self.full_name} ({self.status})"

    # -- money -----------------------------------------------------------
    def recalculate_totals(self):
        chairs = Decimal(self.chairs or 0)
        rate = Decimal(self.rate_per_chair or 0)
        months = Decimal(self.duration or 0)
        self.monthly_payment = chairs * rate
        self.subtotal = self.monthly_payment * months
        self.discount_amount = (self.subtotal * Decimal(self.discount_percent or 0) / 100).quantize(Decimal("0.01"))
        self.grand_total = (
            self.subtotal - self.discount_amount + Decimal(self.security_deposit or 0) + Decimal(self.other_charges or 0)
        )

    def save(self, *args, **kwargs):
        # A full save always refreshes the derived money fields; a targeted
        # save(update_fields=[...]) (status changes etc.) leaves them alone.
        if kwargs.get("update_fields") is None:
            self.recalculate_totals()
        needs_code = self._state.adding and not self.code
        with transaction.atomic():
            super().save(*args, **kwargs)
            if needs_code:
                self.code = f"CWS-{self.pk:04d}"
                # queryset.update() on purpose: no second post_save, so the
                # Reports activity log doesn't get a bogus "updated code" row.
                type(self).objects.filter(pk=self.pk).update(code=self.code)

    # -- billing calendar --------------------------------------------------
    def billing_start(self):
        """Same fallback chain as the page's own monthKeysFrom() call:
        review.effectiveFrom || application.startDate || submittedAt."""
        review = self.review_or_none
        if review and review.effective_from:
            return review.effective_from
        if self.start_date:
            return self.start_date
        return timezone.localtime(self.created_at).date()

    @property
    def review_or_none(self):
        try:
            return self.review
        except CoworkingReview.DoesNotExist:
            return None

    def billing_months(self):
        """["2026-09", "2026-10", ...] -- one per month of `duration`,
        starting from the calendar month the rental begins in."""
        start = self.billing_start()
        keys = []
        for i in range(int(self.duration or 0)):
            index = start.month - 1 + i
            keys.append(f"{start.year + index // 12}-{index % 12 + 1:02d}")
        return keys


class CoworkingMember(models.Model):
    """Section E -- Member / Seat Allocation rows."""

    application = models.ForeignKey(CoworkingApplication, on_delete=models.CASCADE, related_name="members")
    name = models.CharField(max_length=255)
    cnic = models.CharField(max_length=32, blank=True, default="")
    phone = models.CharField(max_length=32, blank=True, default="")
    chair_no = models.CharField(max_length=50, blank=True, default="")
    position = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["position", "id"]

    def __str__(self):
        return f"{self.name} ({self.application.code})"


class CoworkingReview(models.Model):
    """Section J -- "Office Approval & Access Issue" (admin only). One per
    application; created on the first Save Decision and updated after."""

    application = models.OneToOneField(CoworkingApplication, on_delete=models.CASCADE, related_name="review")
    decision = models.CharField(max_length=10, choices=ApplicationStatus.choices, default=ApplicationStatus.PENDING)
    approver_role = models.CharField(max_length=10, choices=ApproverRole.choices, default=ApproverRole.ADMIN)
    approved_by = models.CharField(max_length=255, blank=True, default="")
    designation = models.CharField(max_length=255, blank=True, default="")
    approval_date = models.DateField(null=True, blank=True)

    approved_chairs = models.PositiveSmallIntegerField(null=True, blank=True)
    approved_rate = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    discount = models.DecimalField(max_digits=15, decimal_places=2, null=True, blank=True)
    deposit = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    approved_grand_total = models.DecimalField(max_digits=15, decimal_places=2, null=True, blank=True)

    chair_nos = models.CharField(max_length=100, blank=True, default="")
    access_card_no = models.CharField(max_length=100, blank=True, default="")
    wifi_issued = models.BooleanField(default=False)
    effective_from = models.DateField(null=True, blank=True)
    remarks = models.TextField(blank=True, default="")

    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    reviewed_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.application.code} — {self.decision}"


class CoworkingPayment(models.Model):
    """One "Tick" on the page's Payment Tracking list: rent received for a
    specific calendar month. Always paired 1:1 with the dashboard.Income row
    it created, so the money shows on the Income page too.

    on_delete=CASCADE from the Income side is deliberate: if someone deletes
    that entry on the Income page (fixing a mistake), this tick disappears
    with it and the month goes back to "due" -- instead of the tracker
    saying "paid" for money that's no longer in the ledger. The reverse does
    NOT cascade: deleting an application never deletes received income.
    """

    application = models.ForeignKey(CoworkingApplication, on_delete=models.CASCADE, related_name="payments")
    month = models.CharField(max_length=7, help_text="YYYY-MM the rent covers")
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    method = models.CharField(max_length=20, choices=PaymentMethod.choices)
    income = models.OneToOneField("dashboard.Income", on_delete=models.CASCADE, related_name="coworking_payment")
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]
        indexes = [models.Index(fields=["application", "month"])]

    def __str__(self):
        return f"{self.application.code} {self.month}: {self.amount}"