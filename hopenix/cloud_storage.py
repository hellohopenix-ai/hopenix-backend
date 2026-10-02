"""Default Cloudinary media storage that can also keep zip / office / any
non-image files.

django-cloudinary-storage's MediaCloudinaryStorage always uploads with
resource_type="image". Cloudinary rejects anything that is not a real image
that way ("Invalid image file"), so every .zip/.rar/.docx/audio/... upload to a
module, a task, or a chat failed in production (DEBUG off -> Cloudinary).

This subclass keeps images and PDFs exactly as before (stored as "image", so
every file uploaded earlier is still found at the same name) and sends every
other file type as a Cloudinary "raw" file. Raw files are saved under a
"raw/" sub-folder; that marker stays in the stored name, so opening, downloading
and deleting the file later picks the right resource type again.

Names are also kept short: the FileField columns are varchar(100) and Cloudinary
adds a random suffix to the stored name, so a long file name used to overflow
the column and fail the save.
"""
import posixpath

from cloudinary_storage.storage import MediaCloudinaryStorage

RAW_FOLDER = "raw"
# Stay as "image" on Cloudinary (same as before this storage existed).
IMAGE_EXTENSIONS = {
    "png", "jpg", "jpeg", "jpe", "jfif", "gif", "webp", "bmp", "tif", "tiff",
    "svg", "ico", "heic", "heif", "avif", "pdf",
}
MAX_NAME_LENGTH = 82  # leaves room for Cloudinary's random suffix inside varchar(100)


def _extension(name):
    base = str(name).replace("\\", "/").rsplit("/", 1)[-1]
    return base.rsplit(".", 1)[-1].lower() if "." in base else ""


def _is_raw_name(name):
    return ("/" + str(name).replace("\\", "/")).find("/" + RAW_FOLDER + "/") != -1


class SmartCloudinaryStorage(MediaCloudinaryStorage):
    def _get_resource_type(self, name):
        return "raw" if _is_raw_name(name) else "image"

    def _save(self, name, content):
        name = self._normalise_name(name)
        folder, base = posixpath.split(name)
        ext = _extension(base)
        if ext and ext not in IMAGE_EXTENSIONS and not _is_raw_name(name):
            folder = posixpath.join(folder, RAW_FOLDER) if folder else RAW_FOLDER
        # keep the whole stored name short, never cutting off the extension
        room = MAX_NAME_LENGTH - len(folder) - 1
        if len(base) > room > 8:
            stem, dot, tail = base.rpartition(".")
            if dot and len(tail) < 10:
                base = stem[: max(room - len(tail) - 1, 1)] + "." + tail
            else:
                base = base[:room]
        name = posixpath.join(folder, base) if folder else base
        return super()._save(name, content)
