"""Storage selector for DailyReportFile.file.

Production (settings.USE_CLOUDINARY, i.e. DEBUG off) uses a Cloudinary storage
that understands videos; local development and the test-suite keep using the
normal default (FileSystem) storage. The Cloudinary import happens lazily so
running locally never needs Cloudinary credentials.
"""
from django.conf import settings
from django.core.files.storage import default_storage


def daily_report_storage():
    if getattr(settings, "USE_CLOUDINARY", False):
        from .cloud_storage import DailyReportCloudinaryStorage

        return DailyReportCloudinaryStorage()
    return default_storage
