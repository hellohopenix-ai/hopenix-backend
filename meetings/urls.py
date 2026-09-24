from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    AvailabilityView,
    MeetingRequestViewSet,
    MeetingViewSet,
    RescheduleRequestViewSet,
)

router = DefaultRouter()
router.register(r"meetings", MeetingViewSet, basename="meeting")
router.register(r"requests", MeetingRequestViewSet, basename="meeting-request")
router.register(r"reschedule-requests", RescheduleRequestViewSet, basename="reschedule-request")

urlpatterns = [
    path("availability/", AvailabilityView.as_view(), name="meeting-availability"),
    path("", include(router.urls)),
]
