"""Real storage numbers for Settings -> Storage Usage / Manage Storage.

The right rail used to show a made-up "2.45 GB of 10 GB" (a number kept in
localStorage) and a fake breakdown. This measures what is really stored:

  * production (Cloudinary is the default file storage): every asset in the
    Cloudinary account, via its Admin API — that is where avatars, invoices,
    task zips, message files, ... actually live;
  * local / disk storage: every file under MEDIA_ROOT;
  * in both cases, the private_media folder on the server (applicants' ID
    scans are always kept on disk, never on Cloudinary).

The result is cached for a few minutes because walking a big folder / paging
through Cloudinary on every page view would be slow.
"""
import logging
import os
import time
from pathlib import Path

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

CACHE_KEY = "settings.storage_usage.v1"
CACHE_SECONDS = 5 * 60
CLOUDINARY_MAX_PAGES = 40  # x500 assets — a hard stop so this can never hang a request

CATEGORIES = [
    # key, label, colour used by the UI, extensions
    ("documents", "Documents", "#7c3aed", {"pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "csv", "rtf", "odt", "md", "json"}),
    ("images", "Images", "#0ea5e9", {"jpg", "jpeg", "png", "gif", "webp", "svg", "bmp", "heic", "heif", "ico", "tif", "tiff", "avif"}),
    ("media", "Audio & Video", "#e11d48", {"mp3", "wav", "ogg", "m4a", "aac", "mp4", "mov", "avi", "mkv", "webm", "m4v", "3gp"}),
    ("archives", "Zip & Archives", "#059669", {"zip", "rar", "7z", "tar", "gz", "tgz", "bz2", "cdr"}),
    ("other", "Other", "#64748b", set()),
]
_EXT_TO_KEY = {ext: key for key, _l, _c, exts in CATEGORIES for ext in exts}


def _category_for(ext):
    return _EXT_TO_KEY.get((ext or "").lower().lstrip("."), "other")


def _empty_buckets():
    return {key: {"bytes": 0, "files": 0} for key, *_ in CATEGORIES}


def _scan_disk(root, buckets):
    """Add every file under `root` to `buckets`. Missing folder = nothing."""
    root = Path(root)
    if not root.is_dir():
        return
    for dirpath, _dirs, files in os.walk(root):
        for fname in files:
            try:
                size = os.path.getsize(os.path.join(dirpath, fname))
            except OSError:
                continue
            b = buckets[_category_for(os.path.splitext(fname)[1])]
            b["bytes"] += size
            b["files"] += 1


def _scan_cloudinary(buckets):
    """Sum every Cloudinary asset. Returns (truncated: bool)."""
    import cloudinary.api

    truncated = False
    for resource_type in ("image", "video", "raw"):
        cursor = None
        for page in range(CLOUDINARY_MAX_PAGES):
            kwargs = {"resource_type": resource_type, "type": "upload", "max_results": 500}
            if cursor:
                kwargs["next_cursor"] = cursor
            res = cloudinary.api.resources(**kwargs)
            for asset in res.get("resources", []):
                fmt = asset.get("format") or ""
                if not fmt and resource_type == "video":
                    fmt = "mp4"
                key = _category_for(fmt)
                # videos/raw with an unknown extension still belong in a sensible bucket
                if key == "other" and resource_type == "video":
                    key = "media"
                elif key == "other" and resource_type == "image":
                    key = "images"
                buckets[key]["bytes"] += int(asset.get("bytes") or 0)
                buckets[key]["files"] += 1
            cursor = res.get("next_cursor")
            if not cursor:
                break
        else:
            truncated = True
    return truncated


def _private_root():
    return getattr(settings, "COWORKING_PRIVATE_ROOT", None) or (settings.BASE_DIR / "private_media")


def compute():
    """Fresh measurement (no cache)."""
    buckets = _empty_buckets()
    source = "Server disk"
    note = ""
    truncated = False

    if getattr(settings, "USE_CLOUDINARY", False) and settings.CLOUDINARY_STORAGE.get("CLOUD_NAME"):
        try:
            truncated = _scan_cloudinary(buckets)
            source = "Cloudinary"
        except Exception as exc:  # noqa: BLE001 — bad keys / API rate limit / offline
            logger.warning("Cloudinary usage lookup failed, falling back to disk: %s", exc)
            buckets = _empty_buckets()
            _scan_disk(settings.MEDIA_ROOT, buckets)
            note = "Cloudinary could not be reached, so this shows only files on the server disk."
    else:
        _scan_disk(settings.MEDIA_ROOT, buckets)

    _scan_disk(_private_root(), buckets)  # ID scans etc. — always on the server

    if truncated:
        note = "Very large account — the count is capped, real usage may be higher."

    breakdown = []
    total_bytes = 0
    total_files = 0
    for key, label, colour, _exts in CATEGORIES:
        b = buckets[key]
        total_bytes += b["bytes"]
        total_files += b["files"]
        breakdown.append({"key": key, "label": label, "color": colour, "bytes": b["bytes"], "files": b["files"]})

    return {
        "used_bytes": total_bytes,
        "file_count": total_files,
        "source": source,
        "note": note,
        "breakdown": breakdown,
        "measured_at": int(time.time()),
    }


def get_usage(force=False):
    if not force:
        cached = cache.get(CACHE_KEY)
        if cached:
            return cached
    data = compute()
    cache.set(CACHE_KEY, data, CACHE_SECONDS)
    return data
