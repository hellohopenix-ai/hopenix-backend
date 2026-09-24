from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import VisitorViewSet

router = DefaultRouter()
router.register(r"visitors", VisitorViewSet, basename="visitor")

urlpatterns = [
    path("", include(router.urls)),
]
