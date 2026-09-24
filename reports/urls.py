from django.urls import path

from . import views

# Mounted at /api/reports/ (see hopenix/urls.py).
urlpatterns = [
    # Activity log — everything every user did
    path("activity/", views.ActivityListView.as_view(), name="reports-activity"),
    path("activity/filters/", views.ActivityFiltersView.as_view(), name="reports-activity-filters"),
    path("activity/export/", views.ActivityExportView.as_view(), name="reports-activity-export"),
    path("activity/track/", views.ActivityTrackView.as_view(), name="reports-activity-track"),

    # Per-user reports
    path("users/", views.UserReportsView.as_view(), name="reports-users"),
    path("users/<str:ident>/", views.UserReportDetailView.as_view(), name="reports-user-detail"),

    # Key Summary / charts / "All Reports" rows
    path("summary/", views.SummaryView.as_view(), name="reports-summary"),
    path("catalog/", views.CatalogView.as_view(), name="reports-catalog"),
    path("catalog/<str:key>/", views.CatalogItemView.as_view(), name="reports-catalog-item"),

    # Daily reports (photo/video proof). "files/" and "bulk-delete/" are listed
    # before <int:pk> so they can never be mistaken for an id.
    path("daily/", views.DailyReportListCreateView.as_view(), name="reports-daily"),
    path("daily/bulk-delete/", views.DailyReportBulkDeleteView.as_view(), name="reports-daily-bulk-delete"),
    path("daily/files/<int:file_id>/", views.DailyReportFileView.as_view(), name="reports-daily-file"),
    path("daily/<int:pk>/", views.DailyReportDetailView.as_view(), name="reports-daily-detail"),
    path("daily/<int:pk>/approve/", views.DailyReportApproveView.as_view(), name="reports-daily-approve"),
]
