from django.urls import path

from . import views

urlpatterns = [
    path("", views.EmployeeListView.as_view(), name="employee-list"),
    path("<int:user_id>/", views.EmployeeDetailView.as_view(), name="employee-detail"),
    path("<int:user_id>/status/", views.EmployeeStatusView.as_view(), name="employee-status"),
    path("<int:user_id>/rating/", views.EmployeeRatingView.as_view(), name="employee-rating"),
    path("<int:user_id>/performance/", views.EmployeePerformanceView.as_view(), name="employee-performance"),
    path("<int:user_id>/location/", views.EmployeeLocationView.as_view(), name="employee-location"),

    path("leave-requests/", views.LeaveRequestListCreateView.as_view(), name="leave-request-list"),
    path("leave-requests/<int:leave_id>/approve/", views.LeaveRequestDecideView.as_view(),
         {"decision": "approve"}, name="leave-request-approve"),
    path("leave-requests/<int:leave_id>/reject/", views.LeaveRequestDecideView.as_view(),
         {"decision": "reject"}, name="leave-request-reject"),
    path("leave-requests/<int:leave_id>/", views.LeaveRequestCancelView.as_view(), name="leave-request-cancel"),

    path("holidays/", views.HolidayListCreateView.as_view(), name="holiday-list"),
    path("holidays/<int:holiday_id>/", views.HolidayDeleteView.as_view(), name="holiday-delete"),

    path("announcements/", views.AnnouncementListCreateView.as_view(), name="announcement-list"),
    path("announcements/<int:announcement_id>/", views.AnnouncementDeleteView.as_view(), name="announcement-delete"),
    path("announcements/<int:announcement_id>/seen/", views.AnnouncementSeenView.as_view(), name="announcement-seen"),
]
