from django.contrib import admin

from .models import (
    BillingInfo,
    CompanySettings,
    ExpenseSettings,
    IncomeSettings,
    NotificationPreference,
    ProjectSettings,
    SalesSettings,
    SecuritySetting,
    TaskSettings,
)

admin.site.register(CompanySettings)
admin.site.register(NotificationPreference)
admin.site.register(SecuritySetting)
admin.site.register(BillingInfo)
admin.site.register(ProjectSettings)
admin.site.register(TaskSettings)
admin.site.register(IncomeSettings)
admin.site.register(ExpenseSettings)
admin.site.register(SalesSettings)