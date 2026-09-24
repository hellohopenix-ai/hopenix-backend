from django.contrib import admin

from .models import (
    User,
    OtpCode,
    Profile,
    RolePermission,
    ModulePermission,
    UserAccessOverride,
    UserSubPageAccess,
    AppSetting,
)


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ("email", "name", "role", "status", "date_joined")
    list_filter = ("role", "status")
    search_fields = ("email", "name")


@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    """Lets an admin actually see submitted profiles (phone, CNIC, address,
    education, bank details, CV) somewhere until the app's own Users page
    displays this data directly."""

    list_display = ("user", "phone", "cnic", "city", "country", "salary", "created_at")
    search_fields = ("user__email", "phone", "cnic")


@admin.register(OtpCode)
class OtpCodeAdmin(admin.ModelAdmin):
    list_display = ("email", "code", "created_at", "is_used")
    search_fields = ("email",)


@admin.register(RolePermission)
class RolePermissionAdmin(admin.ModelAdmin):
    list_display = ("role",)


@admin.register(ModulePermission)
class ModulePermissionAdmin(admin.ModelAdmin):
    list_display = ("role", "module", "view", "create", "edit", "delete")
    list_filter = ("role",)


@admin.register(UserAccessOverride)
class UserAccessOverrideAdmin(admin.ModelAdmin):
    list_display = ("user", "mode")


@admin.register(UserSubPageAccess)
class UserSubPageAccessAdmin(admin.ModelAdmin):
    list_display = ("user", "page", "mode")


@admin.register(AppSetting)
class AppSettingAdmin(admin.ModelAdmin):
    list_display = ("key", "value")