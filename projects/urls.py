from django.urls import path
from .views import ProjectViewSet, ModuleViewSet, ModuleFileViewSet

project_list = ProjectViewSet.as_view({"get": "list", "post": "create"})
project_detail = ProjectViewSet.as_view({"get": "retrieve", "put": "partial_update", "patch": "partial_update", "delete": "destroy"})
project_restore = ProjectViewSet.as_view({"post": "restore"})
project_brief_upload = ProjectViewSet.as_view({"post": "upload_brief"})
project_brief_download = ProjectViewSet.as_view({"get": "download_brief"})
project_zip_upload = ProjectViewSet.as_view({"post": "upload_zip", "delete": "upload_zip"})
project_zip_download = ProjectViewSet.as_view({"get": "download_zip"})
project_export_zip = ProjectViewSet.as_view({"get": "export_zip"})
project_zip_files = ProjectViewSet.as_view({"get": "zip_files"})

module_list = ModuleViewSet.as_view({"get": "list", "post": "create"})
module_detail = ModuleViewSet.as_view({"put": "partial_update", "patch": "partial_update", "delete": "destroy"})
module_approve_url = ModuleViewSet.as_view({"post": "approve_url"})

file_list = ModuleFileViewSet.as_view({"get": "list", "post": "create"})
file_delete = ModuleFileViewSet.as_view({"delete": "destroy"})
file_download = ModuleFileViewSet.as_view({"get": "download"})
# FIX: this route was never registered (the paths below are wired by hand, not
# via a router, so the @action on ModuleFileViewSet.approve was unreachable) —
# every "Approved / Show on Portal" tick 404'd and no file could ever be
# approved onto the Client Portal.
file_approve = ModuleFileViewSet.as_view({"post": "approve"})

urlpatterns = [
    path("", project_list, name="project-list"),
    path("zip-files/", project_zip_files, name="project-zip-files"),
    path("<int:pk>/", project_detail, name="project-detail"),
    path("<int:pk>/restore/", project_restore, name="project-restore"),
    path("<int:pk>/brief/", project_brief_upload, name="project-brief-upload"),
    path("<int:pk>/brief/download/", project_brief_download, name="project-brief-download"),
    path("<int:pk>/zip/", project_zip_upload, name="project-zip-upload"),
    path("<int:pk>/zip/download/", project_zip_download, name="project-zip-download"),
    path("<int:pk>/export-zip/", project_export_zip, name="project-export-zip"),

    path("<int:project_pk>/modules/", module_list, name="module-list"),
    path("<int:project_pk>/modules/<int:pk>/", module_detail, name="module-detail"),
    path("<int:project_pk>/modules/<int:pk>/approve-url/", module_approve_url, name="module-approve-url"),

    path("<int:project_pk>/modules/<int:module_pk>/files/", file_list, name="file-list"),
    path("<int:project_pk>/modules/<int:module_pk>/files/<int:pk>/", file_delete, name="file-delete"),
    path("<int:project_pk>/modules/<int:module_pk>/files/<int:pk>/download/", file_download, name="file-download"),
    path("<int:project_pk>/modules/<int:module_pk>/files/<int:pk>/approve/", file_approve, name="file-approve"),
]