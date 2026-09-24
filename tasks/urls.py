from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .views import TaskViewSet

router = DefaultRouter()
router.register(r"tasks", TaskViewSet, basename="task")

# Manually wired (not through the router) so a <file_id> segment can sit
# in the URL — same style projects/urls.py already uses for its nested
# module files. Listed BEFORE the router include so they're matched
# first; they don't collide with the router's own "/tasks/<pk>/" routes
# since those don't have a trailing "/zip/..." part.
task_zip_list = TaskViewSet.as_view({"get": "zip_files", "post": "upload_zip"})
task_zip_download = TaskViewSet.as_view({"get": "download_zip"})
task_zip_delete = TaskViewSet.as_view({"delete": "delete_zip"})

urlpatterns = [
    path("tasks/<int:pk>/zip/", task_zip_list, name="task-zip-list"),
    path("tasks/<int:pk>/zip/<int:file_id>/download/", task_zip_download, name="task-zip-download"),
    path("tasks/<int:pk>/zip/<int:file_id>/", task_zip_delete, name="task-zip-delete"),
    path("", include(router.urls)),
]