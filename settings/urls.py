from django.urls import path

from . import views

app_name = "settings"

urlpatterns = [
    path("company/", views.CompanySettingsView.as_view(), name="company-settings"),
    path("notifications/", views.NotificationPreferencesView.as_view(), name="notification-preferences"),
    path("security/", views.SecuritySettingView.as_view(), name="security-setting"),
    path("billing/", views.BillingInfoView.as_view(), name="billing-info"),
    path("projects/", views.ProjectSettingsView.as_view(), name="project-settings"),
    path("tasks/", views.TaskSettingsView.as_view(), name="task-settings"),
    path("income/", views.IncomeSettingsView.as_view(), name="income-settings"),
    path("expenses/", views.ExpenseSettingsView.as_view(), name="expense-settings"),
    path("sales/", views.SalesSettingsView.as_view(), name="sales-settings"),
    path("notifications/status/", views.NotificationStatusView.as_view(), name="notification-status"),
    path("notifications/test/", views.NotificationTestView.as_view(), name="notification-test"),
    path("branding/", views.BrandingView.as_view(), name="branding"),
    path("company/logo/", views.CompanyLogoView.as_view(), name="company-logo"),
    path("departments/", views.DepartmentListCreateView.as_view(), name="departments"),
    path("departments/<int:pk>/", views.DepartmentDetailView.as_view(), name="department-detail"),
    path("storage/", views.StorageUsageView.as_view(), name="storage-usage"),
    path("change-password/", views.ChangePasswordView.as_view(), name="change-password"),
]