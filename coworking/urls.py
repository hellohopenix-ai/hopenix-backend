from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import CoworkingApplicationViewSet, CoworkingSettingsView

router = DefaultRouter()
router.register(r"applications", CoworkingApplicationViewSet, basename="coworking-application")

urlpatterns = [
    path("settings/", CoworkingSettingsView.as_view(), name="coworking-settings"),
    path("", include(router.urls)),
]