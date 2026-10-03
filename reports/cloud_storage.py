"""Cloudinary storage for daily-report photos AND videos.

django-cloudinary-storage's MediaCloudinaryStorage always uploads with
resource_type="image", so a video sent through it is rejected by Cloudinary
and the whole daily report fails. This subclass picks the resource type from
the stored path instead: DailyReportFile's upload path puts videos under a
".../video/..." folder (see reports.models.daily_report_file_path), and the
returned public_id keeps that folder, so the same rule also works later for
opening and deleting the file. A ".../raw/..." folder (ZIP uploads) means a Cloudinary "raw" file. Everything else stays "image" exactly like
before, so files uploaded earlier are read the same way as always.
"""
from cloudinary_storage.storage import MediaCloudinaryStorage

VIDEO_FOLDER = "/video/"
RAW_FOLDER = "/raw/"


class DailyReportCloudinaryStorage(MediaCloudinaryStorage):
    def _get_resource_type(self, name):
        path = "/" + str(name).replace("\\", "/")
        if VIDEO_FOLDER in path:
            return "video"
        if RAW_FOLDER in path:
            return "raw"  # .zip files
        return "image"  # photos and PDFs, exactly like before
