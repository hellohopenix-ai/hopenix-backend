from django.urls import path

from . import views

# Mounted at /api/flags/ (see hopenix/urls.py).
urlpatterns = [
    path("", views.FlagListView.as_view(), name="userflags-list"),
    path("<str:key>/", views.FlagDetailView.as_view(), name="userflags-detail"),
]
