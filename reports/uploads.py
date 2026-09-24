"""Validation for daily-report photo/video uploads.

The browser-supplied Content-Type (and the file extension) are trivially
faked, so the real type is decided from the file's own first bytes. Only
common image and video containers are accepted — deliberately NOT SVG (it can
carry script) or anything else.
"""

IMAGE_TYPES = {
    "jpeg": "image/jpeg", "png": "image/png", "gif": "image/gif", "webp": "image/webp",
    "bmp": "image/bmp", "heic": "image/heic", "avif": "image/avif",
}
HEIC_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1", b"heif"}


def sniff(head: bytes):
    """Return (kind, content_type) — kind is "image" or "video" — or None if
    the bytes aren't a supported photo/video."""
    if head.startswith(b"\xff\xd8\xff"):
        return "image", IMAGE_TYPES["jpeg"]
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image", IMAGE_TYPES["png"]
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image", IMAGE_TYPES["gif"]
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image", IMAGE_TYPES["webp"]
    if head[:2] == b"BM":
        return "image", IMAGE_TYPES["bmp"]
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand == b"avif":
            return "image", IMAGE_TYPES["avif"]
        if brand in HEIC_BRANDS:
            return "image", IMAGE_TYPES["heic"]
        if brand == b"qt  ":
            return "video", "video/quicktime"
        return "video", "video/mp4"          # mp4 / m4v / 3gp / iso*
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return "video", "video/webm"          # webm / mkv (EBML)
    if head[:4] == b"RIFF" and head[8:12] == b"AVI ":
        return "video", "video/x-msvideo"
    return None


def validate_upload(uploaded, max_bytes):
    """Returns (kind, content_type) or raises ValueError with a message that is
    safe to show the user."""
    if uploaded.size > max_bytes:
        raise ValueError(f"“{uploaded.name}” is larger than {max_bytes // (1024 * 1024)} MB.")
    if uploaded.size == 0:
        raise ValueError(f"“{uploaded.name}” is empty.")
    uploaded.seek(0)
    head = uploaded.read(16)
    uploaded.seek(0)
    found = sniff(head)
    if not found:
        raise ValueError(f"“{uploaded.name}” isn't a supported photo or video (JPG, PNG, WebP, GIF, HEIC, MP4, MOV, WebM).")
    return found
