import re
from collections.abc import Mapping
from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from rest_framework import serializers
from rest_framework.reverse import reverse

from .models import (
    APPLICANT_TYPES,
    DOCUMENT_LIST,
    AccessHours,
    ApplicationStatus,
    ApproverRole,
    BillingCycle,
    CoworkingApplication,
    CoworkingMember,
    CoworkingSettings,
    PaymentMethod,
    Seating,
    total_chairs,
)

# --- The same format rules CoworkingSpacePage.jsx enforces client-side ------
# (re.ASCII so \d means 0-9 only, exactly like the JS regexes -- Python's \d
# would otherwise also accept e.g. Arabic-Indic digits.)
CNIC_RE = re.compile(r"^\d{5}-?\d{7}-?\d{1}$", re.ASCII)
PASSPORT_RE = re.compile(r"^[A-Za-z]{1,2}\d{6,8}$", re.ASCII)
PHONE_RE = re.compile(r"^(\+92|0)?3\d{2}-?\d{7}$", re.ASCII)
WEBSITE_RE = re.compile(r"^https?://.+\..+")

MAX_IMAGE_BYTES = 5 * 1024 * 1024  # the page's own "under 5MB" limit
MAX_DURATION_MONTHS = 60  # sanity bound only; the page's copy says "1 - 12 months"
MAX_MEMBERS = 50


def _is_id_number(value):
    return bool(CNIC_RE.match(value) or PASSPORT_RE.match(value))


def _image_size_ok(upload):
    if upload.size > MAX_IMAGE_BYTES:
        raise serializers.ValidationError("Image is too large — please use a file under 5MB.")


def _required(message, **kwargs):
    """CharField whose required/blank/null errors all read `message` -- the
    same wording the page already shows next to the field."""
    return serializers.CharField(error_messages={"required": message, "blank": message, "null": message}, **kwargs)


def _optional_char(max_length, **kwargs):
    return serializers.CharField(required=False, allow_blank=True, default="", max_length=max_length, **kwargs)


def _image(message, **kwargs):
    return serializers.ImageField(
        validators=[_image_size_ok],
        error_messages={
            "required": message,
            "null": message,
            "empty": "The uploaded file is empty.",
            "invalid_image": "That file isn't a valid image (use JPG or PNG).",
            "invalid": "Upload the image as a file.",
        },
        **kwargs,
    )


class _BlankTolerantSerializer(serializers.Serializer):
    """The page's form state uses "" for every empty optional date/number
    (e.g. startDate: ""), which DRF's DateField/IntegerField would reject as
    malformed. Names listed here are treated as "not provided" instead."""

    blank_as_none = ()
    blank_as_zero = ()

    def to_internal_value(self, data):
        if isinstance(data, Mapping):
            data = dict(data)
            for key in self.blank_as_none:
                if data.get(key) == "":
                    data[key] = None
            for key in self.blank_as_zero:
                if data.get(key) in ("", None):
                    data[key] = 0
        return super().to_internal_value(data)


# ==========================================================================
#  INPUT -- submitting an application (flat, same shape as the page's `form`)
# ==========================================================================
class ApplicationCreateSerializer(_BlankTolerantSerializer):
    """POST /api/coworking/applications/

    Field names are the page's own `form` keys (camelCase) so
    `JSON.stringify(form)` minus the three image fields is a valid payload.
    Money totals (subtotal, discountAmount, grandTotal, monthlyPayment) that
    the page also sends are deliberately NOT declared here: they're
    recomputed server-side from chairs / rate / duration / discount.

    Every error is a flat list of plain strings under the field's name (never
    nested objects), so the page can show them with its existing
    "join all messages" toast.
    """

    blank_as_none = ("applicationDate", "startDate", "dob", "expectedEndDate", "teamSize", "declarationDate")
    blank_as_zero = ("discountPercent", "securityDeposit", "otherCharges")

    # A. Application record
    applicationDate = serializers.DateField(source="application_date", required=False, allow_null=True)
    agreementId = _optional_char(64, source="agreement_id")
    startDate = serializers.DateField(source="start_date", required=False, allow_null=True)

    # B. Applicant / occupant
    fullName = _required("Full name is required", source="full_name", max_length=255)
    fatherName = _optional_char(255, source="father_name")
    cnic = _required("CNIC / Passport No. is required", max_length=32)
    dob = serializers.DateField(required=False, allow_null=True)
    mobile = _required("Mobile No. is required", max_length=32)
    whatsapp = _optional_char(32)
    email = serializers.EmailField(required=False, allow_blank=True, default="", max_length=254)
    address = _required("Residential address is required")
    photo = _image("Upload a photo", required=False, allow_null=True)
    cnicFront = _image("Upload the front of your CNIC", source="cnic_front")
    cnicBack = _image("Upload the back of your CNIC", source="cnic_back")

    # C. Work / business profile
    applicantType = serializers.ListField(
        source="applicant_type",
        child=serializers.ChoiceField(choices=APPLICANT_TYPES),
        allow_empty=False,
        max_length=len(APPLICANT_TYPES),
        error_messages={"required": "Select at least one applicant type", "empty": "Select at least one applicant type"},
    )
    companyName = _optional_char(255, source="company_name")
    legalStatus = _optional_char(100, source="legal_status")
    registrationNo = _optional_char(100, source="registration_no")
    natureOfWork = _optional_char(255, source="nature_of_work")
    website = _optional_char(255)
    teamSize = serializers.IntegerField(
        source="team_size", required=False, allow_null=True, min_value=1,
        error_messages={"min_value": "Team size must be at least 1"},
    )

    # D. Workspace requirements
    chairs = serializers.IntegerField(min_value=1, error_messages={"min_value": "At least 1 chair is required"})
    ratePerChair = serializers.DecimalField(
        source="rate_per_chair", max_digits=10, decimal_places=2, min_value=Decimal("0.01"),
        error_messages={"min_value": "Enter a valid rate"},
    )
    duration = serializers.IntegerField(
        min_value=1, max_value=MAX_DURATION_MONTHS,
        error_messages={
            "min_value": "Duration must be at least 1 month",
            "max_value": f"Duration can't be more than {MAX_DURATION_MONTHS} months",
        },
    )
    expectedEndDate = serializers.DateField(source="expected_end_date", required=False, allow_null=True)
    seating = serializers.ChoiceField(choices=Seating.values, required=False, default=Seating.DEDICATED)
    access = serializers.ChoiceField(choices=AccessHours.values, required=False, default=AccessHours.OFFICE_HOURS)
    assignedChairs = _optional_char(100, source="assigned_chairs")
    specialNotes = _optional_char(5000, source="special_notes")

    # E. Members (validated by hand below so errors stay flat strings)
    members = serializers.ListField(
        child=serializers.DictField(), allow_empty=False, max_length=MAX_MEMBERS,
        error_messages={"required": "Add at least one member/occupant", "empty": "Add at least one member/occupant"},
    )

    # G. Documents & emergency contact
    documents = serializers.ListField(
        child=serializers.ChoiceField(choices=DOCUMENT_LIST), required=False, default=list, max_length=len(DOCUMENT_LIST)
    )
    emergencyName = _required("Emergency contact name is required", source="emergency_name", max_length=255)
    emergencyRelation = _optional_char(100, source="emergency_relation")
    emergencyPhone = _required("Emergency contact phone is required", source="emergency_phone", max_length=32)
    emergencyAltPhone = _optional_char(32, source="emergency_alt_phone")

    # F. Rent & payment summary (inputs only)
    discountPercent = serializers.DecimalField(
        source="discount_percent", max_digits=5, decimal_places=2, min_value=0, max_value=100, required=False, default=0
    )
    securityDeposit = serializers.DecimalField(
        source="security_deposit", max_digits=12, decimal_places=2, min_value=0, required=False, default=0
    )
    otherCharges = serializers.DecimalField(
        source="other_charges", max_digits=12, decimal_places=2, min_value=0, required=False, default=0
    )
    paymentMethod = serializers.ChoiceField(
        source="payment_method", choices=PaymentMethod.values, required=False, default=PaymentMethod.BANK_TRANSFER
    )
    transactionNo = _optional_char(100, source="transaction_no")
    billingDueDay = _optional_char(20, source="billing_due_day")
    billingCycle = serializers.ChoiceField(
        source="billing_cycle", choices=BillingCycle.values, required=False, default=BillingCycle.MONTHLY
    )

    # H / I. Terms & declaration
    termsAgreed = serializers.BooleanField(source="terms_agreed", required=False, default=False)
    declarationName = _required("Type your name to sign the declaration", source="declaration_name", max_length=255)
    declarationDate = serializers.DateField(source="declaration_date", required=False, allow_null=True)

    # -- field-level rules ---------------------------------------------------
    def validate_cnic(self, value):
        if not _is_id_number(value):
            raise serializers.ValidationError("Use CNIC format 12345-1234567-1, or a valid passport number")
        return value

    def validate_mobile(self, value):
        if not PHONE_RE.match(value):
            raise serializers.ValidationError("Use format 03XX-XXXXXXX")
        return value

    def validate_whatsapp(self, value):
        if value and not PHONE_RE.match(value):
            raise serializers.ValidationError("Use format 03XX-XXXXXXX")
        return value

    def validate_emergencyPhone(self, value):
        if not PHONE_RE.match(value):
            raise serializers.ValidationError("Use format 03XX-XXXXXXX")
        return value

    def validate_website(self, value):
        if value and not WEBSITE_RE.match(value):
            raise serializers.ValidationError("Include https:// and a valid domain")
        return value

    def validate_chairs(self, value):
        limit = total_chairs()
        if value > limit:
            raise serializers.ValidationError(f"Only {limit} chairs total are available")
        return value

    def validate_termsAgreed(self, value):
        if not value:
            raise serializers.ValidationError("Please accept the terms & conditions")
        return value

    def validate_members(self, rows):
        cleaned, problems = [], []
        for i, row in enumerate(rows, start=1):
            name = str(row.get("name") or "").strip()
            cnic = str(row.get("cnic") or "").strip()
            phone = str(row.get("phone") or "").strip()
            chair_no = str(row.get("chairNo") or "").strip()
            if not (name or cnic or phone or chair_no):
                continue  # fully blank row -- the page ignores these too
            if not name:
                problems.append(f"Member {i}: name required")
            if cnic and not _is_id_number(cnic):
                problems.append(f"Member {i}: invalid CNIC/passport format")
            if phone and not PHONE_RE.match(phone):
                problems.append(f"Member {i}: invalid phone format")
            if len(name) > 255 or len(cnic) > 32 or len(phone) > 32 or len(chair_no) > 50:
                problems.append(f"Member {i}: a value is too long")
            cleaned.append({"name": name, "cnic": cnic, "phone": phone, "chair_no": chair_no})
        if problems:
            raise serializers.ValidationError(problems)
        if not cleaned:
            raise serializers.ValidationError("Add at least one member/occupant")
        return cleaned

    # -- cross-field rule ----------------------------------------------------
    def validate(self, attrs):
        types = attrs.get("applicant_type", [])
        if ("Company" in types or "Agency" in types) and not attrs.get("company_name", "").strip():
            raise serializers.ValidationError(
                {"companyName": ["Company / Agency name is required for this applicant type"]}
            )
        return attrs

    @transaction.atomic
    def create(self, validated):
        request = self.context.get("request")
        user = getattr(request, "user", None)
        members = validated.pop("members")

        # dict.fromkeys = de-duplicate, keep the order the user picked them in
        validated["applicant_type"] = list(dict.fromkeys(validated["applicant_type"]))
        validated["documents"] = list(dict.fromkeys(validated.get("documents", [])))
        for key in ("application_date", "declaration_date"):
            if not validated.get(key):
                validated[key] = timezone.localdate()

        application = CoworkingApplication.objects.create(
            created_by=user if getattr(user, "is_authenticated", False) else None, **validated
        )
        CoworkingMember.objects.bulk_create(
            CoworkingMember(application=application, position=i, **m) for i, m in enumerate(members)
        )
        return application


# ==========================================================================
#  INPUT -- Section J, office decision (admin only)
# ==========================================================================
class ReviewInputSerializer(_BlankTolerantSerializer):
    """POST /api/coworking/applications/<code>/review/ -- the ReviewPanel's
    own `review` state object, as-is. Unknown keys (reviewedBy, reviewedAt
    echoed back from a previous save) are ignored."""

    blank_as_none = (
        "approvalDate", "effectiveFrom", "approvedChairs", "approvedRate", "discount", "deposit", "approvedGrandTotal",
    )

    decision = serializers.ChoiceField(choices=ApplicationStatus.values)
    approverRole = serializers.ChoiceField(
        source="approver_role", choices=ApproverRole.values, required=False, default=ApproverRole.ADMIN
    )
    approvedBy = _optional_char(255, source="approved_by")
    designation = _optional_char(255)
    approvalDate = serializers.DateField(source="approval_date", required=False, allow_null=True)
    approvedChairs = serializers.IntegerField(source="approved_chairs", required=False, allow_null=True, min_value=0)
    approvedRate = serializers.DecimalField(
        source="approved_rate", max_digits=10, decimal_places=2, min_value=0, required=False, allow_null=True
    )
    discount = serializers.DecimalField(max_digits=15, decimal_places=2, min_value=0, required=False, allow_null=True)
    deposit = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0, required=False, allow_null=True)
    approvedGrandTotal = serializers.DecimalField(
        source="approved_grand_total", max_digits=15, decimal_places=2, min_value=0, required=False, allow_null=True
    )
    chairNos = _optional_char(100, source="chair_nos")
    accessCardNo = _optional_char(100, source="access_card_no")
    wifiIssued = serializers.BooleanField(source="wifi_issued", required=False, default=False)
    effectiveFrom = serializers.DateField(source="effective_from", required=False, allow_null=True)
    remarks = _optional_char(5000)

    def validate_approvedChairs(self, value):
        limit = total_chairs()
        if value is not None and value > limit:
            raise serializers.ValidationError(f"Only {limit} chairs total are available")
        return value


class PaymentInputSerializer(serializers.Serializer):
    """POST /api/coworking/applications/<code>/payments/ -- one "Tick"."""

    month = serializers.RegexField(
        r"^\d{4}-(0[1-9]|1[0-2])$", error_messages={"invalid": "month must look like 2026-09"}
    )
    amount = serializers.DecimalField(
        max_digits=12, decimal_places=2, min_value=Decimal("0.01"),
        error_messages={
            "min_value": "Enter a valid payment amount.",
            "invalid": "Enter a valid payment amount.",
            "required": "Enter a valid payment amount.",
        },
    )
    # The page has no per-payment method picker yet, so this normally isn't
    # sent and the application's own paymentMethod is used.
    method = serializers.ChoiceField(choices=PaymentMethod.values, required=False)


# ==========================================================================
#  OUTPUT -- one record, in exactly the shape the page already keeps in state
# ==========================================================================
def _iso(value):
    return value.isoformat() if value else ""


def _num(value):
    """Decimal -> plain JSON number (DRF's encoder would turn a raw Decimal
    into a string, and the page does arithmetic on these)."""
    if value is None:
        return ""
    value = Decimal(value)
    return int(value) if value == value.to_integral_value() else float(value)


class ApplicationRecordSerializer(serializers.Serializer):
    """{ id, status, submittedAt, application:{...}, review, payments:[...],
    billingMonths } -- i.e. the same `record` object CoworkingSpacePage.jsx
    used to build for localStorage, so list/POST/review/payments responses
    can be dropped straight into its `records` state.

    Conventions inherited from the page: an empty optional date/text/number
    comes out as "" (not null), so React's controlled inputs never see null.
    `id` is the human form number ("CWS-0007"), which is also what the URLs
    use to look a record up.

    photo / cnicFront / cnicBack are NOT inlined (the old localStorage
    version stored base64 blobs here). They come back as authenticated API
    URLs (`photoUrl`, `cnicFrontUrl`, `cnicBackUrl`, or null): fetch them
    with the Authorization header and use the blob -- a bare <img src>
    can't send a token, which is intentional for ID documents.
    """

    def to_representation(self, app):
        request = self.context.get("request")
        review = app.review_or_none
        return {
            "id": app.code,
            "status": app.status,
            "submittedAt": app.created_at.isoformat(),
            "createdBy": getattr(app.created_by, "name", "") or "",
            "application": self._application(app, request),
            "review": self._review(review) if review else None,
            "payments": [
                {
                    "id": p.pk,
                    "amount": _num(p.amount),
                    "date": p.created_at.isoformat(),
                    "month": p.month,
                    "method": p.method,
                }
                for p in app.payments.all()
            ],
            "billingMonths": app.billing_months(),
        }

    @staticmethod
    def _file_url(app, request, kind, field):
        if not field:
            return None
        return reverse("coworking-application-files", kwargs={"code": app.code, "kind": kind}, request=request)

    def _application(self, a, request):
        return {
            "formNo": a.code,
            "applicationDate": _iso(a.application_date),
            "agreementId": a.agreement_id,
            "startDate": _iso(a.start_date),
            "fullName": a.full_name,
            "fatherName": a.father_name,
            "cnic": a.cnic,
            "dob": _iso(a.dob),
            "mobile": a.mobile,
            "whatsapp": a.whatsapp,
            "email": a.email,
            "address": a.address,
            "photoUrl": self._file_url(a, request, "photo", a.photo),
            "cnicFrontUrl": self._file_url(a, request, "cnic-front", a.cnic_front),
            "cnicBackUrl": self._file_url(a, request, "cnic-back", a.cnic_back),
            "applicantType": list(a.applicant_type or []),
            "companyName": a.company_name,
            "legalStatus": a.legal_status,
            "registrationNo": a.registration_no,
            "natureOfWork": a.nature_of_work,
            "website": a.website,
            "teamSize": "" if a.team_size is None else a.team_size,
            "chairs": a.chairs,
            "ratePerChair": _num(a.rate_per_chair),
            "duration": a.duration,
            "expectedEndDate": _iso(a.expected_end_date),
            "seating": a.seating,
            "access": a.access,
            "assignedChairs": a.assigned_chairs,
            "specialNotes": a.special_notes,
            "members": [
                {"id": str(m.pk), "name": m.name, "cnic": m.cnic, "phone": m.phone, "chairNo": m.chair_no}
                for m in a.members.all()
            ],
            "documents": list(a.documents or []),
            "emergencyName": a.emergency_name,
            "emergencyRelation": a.emergency_relation,
            "emergencyPhone": a.emergency_phone,
            "emergencyAltPhone": a.emergency_alt_phone,
            "discountPercent": _num(a.discount_percent),
            "securityDeposit": _num(a.security_deposit),
            "otherCharges": _num(a.other_charges),
            "paymentMethod": a.payment_method,
            "transactionNo": a.transaction_no,
            "billingDueDay": a.billing_due_day,
            "billingCycle": a.billing_cycle,
            "termsAgreed": a.terms_agreed,
            "declarationName": a.declaration_name,
            "declarationDate": _iso(a.declaration_date),
            "subtotal": _num(a.subtotal),
            "discountAmount": _num(a.discount_amount),
            "grandTotal": _num(a.grand_total),
            "monthlyPayment": _num(a.monthly_payment),
        }

    @staticmethod
    def _review(r):
        return {
            "decision": r.decision,
            "approverRole": r.approver_role,
            "approvedBy": r.approved_by,
            "designation": r.designation,
            "approvalDate": _iso(r.approval_date),
            "approvedChairs": "" if r.approved_chairs is None else r.approved_chairs,
            "approvedRate": _num(r.approved_rate),
            "discount": _num(r.discount),
            "deposit": _num(r.deposit),
            "approvedGrandTotal": _num(r.approved_grand_total),
            "chairNos": r.chair_nos,
            "accessCardNo": r.access_card_no,
            "wifiIssued": r.wifi_issued,
            "effectiveFrom": _iso(r.effective_from),
            "remarks": r.remarks,
            "reviewedBy": getattr(r.reviewed_by, "name", "") or "",
            "reviewedAt": r.reviewed_at.isoformat() if r.reviewed_at else "",
        }


class CoworkingSettingsSerializer(serializers.ModelSerializer):
    """The 4 hero stat cards -- camelCase field names to match every other
    payload CoworkingSpacePage.jsx already reads (see ApplicationRecordSerializer
    above), so the frontend needs no remapping."""

    totalChairs = serializers.IntegerField(source="total_chairs", min_value=0, max_value=1000)
    monthlyRate = serializers.DecimalField(source="monthly_rate", max_digits=10, decimal_places=2, min_value=0)
    minDurationMonths = serializers.IntegerField(source="min_duration_months", min_value=1, max_value=120)
    maxDurationMonths = serializers.IntegerField(source="max_duration_months", min_value=1, max_value=120)
    durationNote = serializers.CharField(source="duration_note", allow_blank=True, max_length=100, required=False)
    securityFeatures = serializers.CharField(source="security_features", allow_blank=True, max_length=150, required=False)
    supportHours = serializers.CharField(source="support_hours", allow_blank=True, max_length=50, required=False)
    updatedAt = serializers.DateTimeField(source="updated_at", read_only=True)

    class Meta:
        model = CoworkingSettings
        fields = [
            "totalChairs",
            "monthlyRate",
            "minDurationMonths",
            "maxDurationMonths",
            "durationNote",
            "securityFeatures",
            "supportHours",
            "updatedAt",
        ]

    def validate(self, attrs):
        min_m = attrs.get("min_duration_months", getattr(self.instance, "min_duration_months", 1))
        max_m = attrs.get("max_duration_months", getattr(self.instance, "max_duration_months", 12))
        if min_m > max_m:
            raise serializers.ValidationError({"minDurationMonths": ["Minimum duration can't be more than the maximum."]})
        return attrs