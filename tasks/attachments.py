"""Helpers to keep `Task.attachments` free of duplicate entries.

Why duplicates happened: uploading a zip / file from the Tasks page writes the
same attachment TWICE into `task.attachments`:
  1. the upload endpoint itself (upload_zip / upload_file_attachment) appends
     an entry, and
  2. the frontend then calls add-attachment / complete with its own copy of
     the same file, which was appended blindly.
So the admin saw every uploaded file two times. Both entries point at the same
stored file, so they are merged into one here.
"""


def _norm_zip_id(value):
    """Backend stores zipFileId as int (5), frontend as "task-5" — same zip."""
    if value in (None, ""):
        return None
    return str(value).replace("task-", "", 1)


def attachment_key(a):
    """Identity of an attachment, or None if it can't be safely compared."""
    if not isinstance(a, dict):
        return None
    zip_id = _norm_zip_id(a.get("zipFileId"))
    if zip_id is not None:
        return f"zip:{zip_id}"
    if a.get("id") not in (None, ""):
        return f"id:{a.get('id')}"
    return None


def merge_attachments(existing, incoming=()):
    """existing + incoming, with entries for the same file merged into one.

    The merged entry keeps every non-empty field of both copies (the frontend
    copy carries `id` + `url`, the backend copy carries the numeric
    `zipFileId`), placed at the position of the first copy.
    """
    result = []
    index = {}
    for a in [*(existing or []), *(incoming or [])]:
        key = attachment_key(a)
        if key is None:
            result.append(a)
            continue
        if key in index:
            pos = index[key]
            merged = dict(result[pos])
            merged.update({k: v for k, v in a.items() if v not in (None, "")})
            result[pos] = merged
        else:
            index[key] = len(result)
            result.append(a)
    return result
