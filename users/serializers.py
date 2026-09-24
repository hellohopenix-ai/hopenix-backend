from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from .models import (
    Profile,
    RolePermission,
    ModulePermission,
    UserAccessOverride,
    UserSubPageAccess,
    AppSetting,
)

User = get_user_model()


class UserSerializer(serializers.ModelSerializer):
    """What the frontend receives to build its `user` object in AuthContext,
    AND what UserPage.jsx's detail modal reads for bio-data. Field names
    here are deliberately camelCase (fatherName, maritalStatus, ...) to
    match UserPage.jsx's rawUser.* reads exactly - they're pulled from the
    related Profile model (snake_case there) via `source=`. If a user
    hasn't submitted CompleteProfilePage yet, obj.profile doesn't exist —
    DRF safely returns None for all of these in that case (see
    profileCompleted below to detect that case explicitly)."""

    profileCompleted = serializers.SerializerMethodField()

    fatherName = serializers.CharField(source="profile.father_name", read_only=True)
    dob = serializers.CharField(source="profile.dob", read_only=True)
    dateOfBirth = serializers.CharField(source="profile.dob", read_only=True)  # alias: EmployeesPage/TeamBirthdayConfetti/BirthdayCelebration read this exact name
    gender = serializers.CharField(source="profile.gender", read_only=True)
    maritalStatus = serializers.CharField(source="profile.marital_status", read_only=True)
    phone = serializers.CharField(source="profile.phone", read_only=True)
    cnic = serializers.CharField(source="profile.cnic", read_only=True)
    emergencyContact = serializers.CharField(source="profile.emergency_contact", read_only=True)
    currentAddress = serializers.CharField(source="profile.current_address", read_only=True)
    permanentAddress = serializers.CharField(source="profile.permanent_address", read_only=True)
    city = serializers.CharField(source="profile.city", read_only=True)
    country = serializers.CharField(source="profile.country", read_only=True)

    education = serializers.JSONField(source="profile.education", read_only=True)
    totalExperience = serializers.CharField(source="profile.total_experience", read_only=True)
    experience = serializers.JSONField(source="profile.experience", read_only=True)
    workTypes = serializers.JSONField(source="profile.work_types", read_only=True)
    languages = serializers.JSONField(source="profile.languages", read_only=True)
    programmingLanguages = serializers.JSONField(source="profile.programming_languages", read_only=True)
    skills = serializers.JSONField(source="profile.skills", read_only=True)

    bankName = serializers.CharField(source="profile.bank_name", read_only=True)
    accountTitle = serializers.CharField(source="profile.account_title", read_only=True)
    accountNumber = serializers.CharField(source="profile.account_number", read_only=True)
    iban = serializers.CharField(source="profile.iban", read_only=True)
    branchCode = serializers.CharField(source="profile.branch_code", read_only=True)

    # Admin-set compensation — see AdminUpdateProfileView / handleModalSalaryUpdate.
    salary = serializers.DecimalField(source="profile.salary", max_digits=12, decimal_places=2, read_only=True, default=None)

    cvFileName = serializers.SerializerMethodField()
    cvDataUrl = serializers.SerializerMethodField()
    avatar = serializers.SerializerMethodField()
    registrationPhoto = serializers.SerializerMethodField()
    idFrontUrl = serializers.SerializerMethodField()
    idBackUrl = serializers.SerializerMethodField()
    updated_at = serializers.SerializerMethodField()

    managerId = serializers.IntegerField(source="manager_id", read_only=True)
    managerName = serializers.CharField(source="manager.name", read_only=True, default=None)

    class Meta:
        model = User
        fields = [
            "id", "name", "email", "company", "role", "department", "status",
            "date_joined", "profileCompleted", "managerId", "managerName", "avatar", "registrationPhoto", "updated_at",
            "fatherName", "dob", "dateOfBirth", "gender", "maritalStatus", "phone", "cnic",
            "emergencyContact", "currentAddress", "permanentAddress", "city", "country",
            "education", "totalExperience", "experience", "workTypes", "languages",
            "programmingLanguages", "skills",
            "bankName", "accountTitle", "accountNumber", "iban", "branchCode", "salary",
            "cvFileName", "cvDataUrl", "idFrontUrl", "idBackUrl",
        ]

    def get_profileCompleted(self, obj):
        return hasattr(obj, "profile")

    def get_updated_at(self, obj):
        profile = getattr(obj, "profile", None)
        if profile and profile.updated_at:
            return int(profile.updated_at.timestamp())
        return None

    def get_cvFileName(self, obj):
        profile = getattr(obj, "profile", None)
        if profile and profile.cv_file:
            return profile.cv_file.name.rsplit("/", 1)[-1]
        return None

    def get_cvDataUrl(self, obj):
        profile = getattr(obj, "profile", None)
        if not (profile and profile.cv_file):
            return None
        request = self.context.get("request")
        url = profile.cv_file.url
        return request.build_absolute_uri(url) if request else url

    def get_avatar(self, obj):
        request = self.context.get("request")
        # Self-editable avatar (Settings, any account) takes priority;
        # profile.profile_photo is the one-time registration photo and is
        # the fallback for accounts that set that but never touched
        # Settings since.
        source = obj.avatar or getattr(getattr(obj, "profile", None), "profile_photo", None)
        if not source:
            return None
        url = source.url
        full_url = request.build_absolute_uri(url) if request else url
        profile = getattr(obj, "profile", None)
        if profile and profile.updated_at:
            ts = int(profile.updated_at.timestamp())
            return f"{full_url}?v={ts}"
        return full_url

    def get_registrationPhoto(self, obj):
        profile = getattr(obj, "profile", None)
        if not (profile and profile.registration_photo):
            return None
        request = self.context.get("request")
        url = profile.registration_photo.url
        full_url = request.build_absolute_uri(url) if request else url
        if profile.updated_at:
            ts = int(profile.updated_at.timestamp())
            return f"{full_url}?v={ts}"
        return full_url

    def get_idFrontUrl(self, obj):
        profile = getattr(obj, "profile", None)
        if not (profile and profile.id_card_front):
            return None
        request = self.context.get("request")
        url = profile.id_card_front.url
        return request.build_absolute_uri(url) if request else url

    def get_idBackUrl(self, obj):
        profile = getattr(obj, "profile", None)
        if not (profile and profile.id_card_back):
            return None
        request = self.context.get("request")
        url = profile.id_card_back.url
        return request.build_absolute_uri(url) if request else url


class RegisterSerializer(serializers.ModelSerializer):
    # min_length is a cheap first check; validate_password below runs
    # Django's full AUTH_PASSWORD_VALIDATORS (common-password list,
    # all-numeric check, similarity to name/email, etc.) so weak
    # passwords like "password123" or the user's own name are rejected
    # even though they're 8+ characters.
    password = serializers.CharField(write_only=True, min_length=8, trim_whitespace=False)

    class Meta:
        model = User
        fields = ["name", "email", "password", "company", "department"]

    def validate_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Name is required.")
        return value

    def validate_email(self, value):
        value = value.strip().lower()
        if User.objects.filter(email=value).exists():
            # Deliberately vague — doesn't confirm/deny whether this
            # exact email has an account, which would let someone enumerate
            # registered users. (This one intentionally differs: it's the
            # standard, expected UX for "email already taken" on a signup
            # form, not a login attempt.)
            raise serializers.ValidationError("An account with this email already exists.")
        return value

    def validate_password(self, value):
        # Run against a dummy, not-yet-saved user so the similarity
        # validator can compare the password to name/email already
        # entered in this form.
        temp_user = User(
            email=self.initial_data.get("email", ""),
            name=self.initial_data.get("name", ""),
        )
        try:
            validate_password(value, user=temp_user)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(list(exc.messages))
        return value

    def create(self, validated_data):
        password = validated_data.pop("password")
        # Always starts as employee/pending, same rule as the old
        # local registerUser() in AuthContext.jsx.
        user = User(
            **validated_data,
            role="employee",
            status="pending",
        )
        user.set_password(password)
        user.save()
        return user


class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)


class ProfileSerializer(serializers.ModelSerializer):
    """Used by CompleteProfileView. education/experience/work_types/
    languages/programming_languages/skills arrive as JSON-encoded strings
    inside multipart form data (since FormData can't send real arrays),
    so the view decodes those before handing data to this serializer —
    by the time this validates, they're already plain Python lists.

    Also reused (with a restricted, admin-only field subset) by
    AdminUpdateProfileView for salary/bank-detail edits — see that view
    for the allow-list."""

    class Meta:
        model = Profile
        fields = [
            "father_name", "dob", "gender", "marital_status", "phone", "cnic",
            "emergency_contact", "current_address", "permanent_address", "city",
            "country", "education", "total_experience", "experience", "work_types",
            "languages", "programming_languages", "skills", "cv_file", "profile_photo",
            "id_card_front", "id_card_back",
            "bank_name", "account_title", "account_number", "iban", "branch_code",
            "salary",
        ]
        extra_kwargs = {
            "cv_file": {"required": False},  # required only on first submission — enforced in the view
            "profile_photo": {"required": False},  # required only on first submission — enforced in the view
            "id_card_front": {"required": False},
            "id_card_back": {"required": False},
            "salary": {"required": False},
        }

    def validate_phone(self, value):
        qs = Profile.objects.filter(phone=value)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("This phone number is already registered to another account.")
        return value

    def validate_cnic(self, value):
        if not value:
            return value
        qs = Profile.objects.filter(cnic=value)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("This CNIC is already registered to another account.")
        return value


class RolePermissionSerializer(serializers.ModelSerializer):
    class Meta:
        model = RolePermission
        fields = ["role", "pages"]


class ModulePermissionSerializer(serializers.ModelSerializer):
    class Meta:
        model = ModulePermission
        fields = ["role", "module", "view", "create", "edit", "delete"]


class UserAccessOverrideSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserAccessOverride
        fields = ["mode", "pages"]


class UserSubPageAccessSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserSubPageAccess
        fields = ["page", "mode"]


class AppSettingSerializer(serializers.ModelSerializer):
    class Meta:
        model = AppSetting
        fields = ["key", "value"]