import os

from django.conf import settings
from django.core.files.storage import FileSystemStorage


class PrivateMediaStorage(FileSystemStorage):
    """Where applicants' photo + CNIC scans live.

    Everything else in this project (CVs, receipts, avatars...) is written
    under MEDIA_ROOT, which the dev server -- and typically nginx in
    production -- serves to anyone who knows the URL. National-ID scans
    shouldn't work like that, so these files are kept in a separate folder
    (`<project>/private_media/` by default, override with
    COWORKING_PRIVATE_ROOT in settings) that is NOT under MEDIA_URL. The
    only way to read them back is the authenticated endpoint
    GET /api/coworking/applications/<code>/files/<kind>/ (see views.py).

    The location is resolved lazily on every access (not frozen at import
    time) so tests / settings overrides can point it somewhere temporary.
    """

    @property
    def base_location(self):
        return getattr(settings, "COWORKING_PRIVATE_ROOT", None) or (settings.BASE_DIR / "private_media")

    @property
    def location(self):
        return os.path.abspath(self.base_location)

    def url(self, name):
        raise ValueError(
            "Coworking ID documents are private and have no public URL -- "
            "use the authenticated /files/<kind>/ endpoint instead."
        )


def get_private_storage():
    return PrivateMediaStorage()
