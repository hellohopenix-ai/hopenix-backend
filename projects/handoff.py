"""Hand-off between the members of a project, WITH admin approval.

Employees are not allowed to message each other, so the work never goes
straight from one member to the next:

1. Whenever a member attaches a NEW file or link to a module (or marks the
   module Completed), the ADMIN(s) get ONE chat message for it - each file
   and each link is announced exactly once (admin_notified_at /
   handoff_url_notified), no matter how many pages or saves it goes through.
2. An admin presses "Approve & send" (a button on the module - ticking the
   module is NOT an approval). Everything not yet forwarded (forwarded_at /
   handoff_url_forwarded) goes from the admin to the NEXT member, with
   "please continue with <next module>".
3. Attach something new later -> back to step 1; approve again -> only the
   new items are forwarded.

Never allowed to fail the action that triggered it.
"""
import logging

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.utils import timezone

from .models import Module, ModuleStatusChoices

logger = logging.getLogger(__name__)

MAX_FILES = 10  # newest files only, so one action can never flood a chat
MAX_FILE_BYTES = 100 * 1024 * 1024  # bigger ones are named in the text instead


def find_next_module(module, ref_user_id):
    """The next module in the same project (same parent, creation order)
    that has an assignee other than whoever just finished and is not
    already completed."""
    siblings = (
        Module.objects.filter(project_id=module.project_id, parent_id=module.parent_id)
        .select_related("assignee")
        .order_by("created_at", "id")
    )
    seen = False
    for m in siblings:
        if m.id == module.id:
            seen = True
            continue
        if not seen:
            continue
        if m.assignee_id and m.assignee_id != ref_user_id and m.status != ModuleStatusChoices.COMPLETED:
            return m
    return None


def _deliver(sender, recipient, request, text="", content=None, name="", size=None, mime=""):
    """Same steps as messaging.views.SendMessageView (save, unread count,
    push notification, websocket event) for one message."""
    from django.db import models
    from messaging.models import Message, Participant
    from messaging.push_utils import send_new_message_push
    from messaging.serializers import MessageSerializer
    from messaging.views import get_or_create_direct_conversation, push_to_user

    kind = "text"
    if content is not None:
        kind = "image" if (mime or "").startswith("image/") else "file"
    conv = get_or_create_direct_conversation(sender, recipient)
    msg = Message.objects.create(
        conversation=conv,
        sender=sender,
        recipient=recipient,
        kind=kind,
        text=text,
        attachment=content,
        attachment_name=name if content is not None else "",
        attachment_size=size if content is not None else None,
    )
    conv.updated_at = timezone.now()
    conv.save(update_fields=["updated_at"])
    Participant.objects.filter(conversation=conv, user=recipient).update(
        hidden=False, unread_count=models.F("unread_count") + 1
    )
    Participant.objects.filter(conversation=conv, user=sender).update(hidden=False)

    data = MessageSerializer(msg, context={"request": request}).data
    try:
        send_new_message_push(recipient, data, sender.name)
    except Exception:  # noqa: BLE001
        logger.exception("Handoff push to user %s failed", recipient.id)
    event = {
        "type": "message.new", "conversation_id": conv.id,
        "sender_id": sender.id, "recipient_id": recipient.id, "message": data,
    }
    for target_id in (recipient.id, sender.id):
        try:
            push_to_user(target_id, event)
        except Exception:  # noqa: BLE001
            logger.exception("Handoff websocket event to user %s failed", target_id)


def pending_items(module):
    """(unforwarded files queryset, link-not-forwarded-yet?)"""
    files = module.files.filter(forwarded_at__isnull=True)
    url = (module.url or "").strip()
    url_pending = bool(url) and url != (module.handoff_url_forwarded or "")
    return files, url_pending


def has_pending(module):
    files, url_pending = pending_items(module)
    return url_pending or files.exists()


def _admins(exclude_id):
    User = get_user_model()
    return list(User.objects.filter(role="admin", status="approved", is_active=True).exclude(id=exclude_id))


def _read_file(mf):
    mf.file.open("rb")
    try:
        return mf.file.read()
    finally:
        mf.file.close()


def _approve_hint(module):
    nxt = find_next_module(module, module.assignee_id) if module.status == ModuleStatusChoices.COMPLETED else None
    if nxt is None or nxt.assignee is None:
        return ""
    return (
        f'⏳ Waiting for your approval to send this to {nxt.assignee.name} for "{nxt.name}". '
        'Press "Approve & send" on the module (Projects page or Tasks page).'
    )


def _announce_to_admins(module, actor, request, files, link_url, headline):
    """One message (+ the given files) from `actor` to every admin."""
    admins = _admins(actor.id)
    lines = [headline]
    if link_url:
        lines.append(f"🔗 Link: {link_url}")
    if files:
        lines.append(f"📎 {len(files)} file(s) attached below.")
    hint = _approve_hint(module)
    if hint:
        lines.append(hint)
    text = "\n".join(lines)
    for admin in admins:
        try:
            _deliver(actor, admin, request, text=text)
            for mf in files:
                if (mf.size or 0) > MAX_FILE_BYTES:
                    continue
                data = _read_file(mf)
                _deliver(
                    actor, admin, request,
                    content=ContentFile(data, name=mf.original_name),
                    name=mf.original_name, size=len(data), mime=mf.mime_type,
                )
        except Exception:  # noqa: BLE001
            logger.exception("Handoff: announcing to admin %s failed", admin.id)


def notify_new_file(module_file, actor, request):
    """A file was just attached to a module -> tell the admin, once."""
    try:
        from .models import ModuleFile

        if not ModuleFile.objects.filter(pk=module_file.pk, admin_notified_at__isnull=True).update(
            admin_notified_at=timezone.now()
        ):
            return
        if actor is None or actor.role == "admin":
            return
        module = module_file.module
        _announce_to_admins(
            module, actor, request, [module_file], "",
            f'📎 {actor.name} attached "{module_file.original_name}" to "{module.name}" ({module.project.name}).',
        )
    except Exception:  # noqa: BLE001
        logger.exception("Handoff: notify_new_file failed for %s", getattr(module_file, "pk", None))


def notify_new_link(module, url, actor, request):
    """A link was just set on a module -> tell the admin, once per link."""
    try:
        url = (url or "").strip()
        if not url:
            return
        if not Module.objects.filter(pk=module.pk).exclude(handoff_url_notified=url).update(handoff_url_notified=url):
            return
        if actor is None or actor.role == "admin":
            return
        module.refresh_from_db()
        _announce_to_admins(
            module, actor, request, [], url,
            f'🔗 {actor.name} attached a link to "{module.name}" ({module.project.name}).',
        )
    except Exception:  # noqa: BLE001
        logger.exception("Handoff: notify_new_link failed for %s", getattr(module, "pk", None))


def _forward_to_next(module, approver, next_module, request):
    recipient = next_module.assignee
    if recipient is None or recipient.id == approver.id:
        return False
    files_qs, url_pending = pending_items(module)
    files = list(files_qs.order_by("uploaded_at", "id"))
    if not files and not url_pending:
        return False
    url = (module.url or "").strip()
    # Claim everything first (once only), then send.
    now = timezone.now()
    from .models import ModuleFile

    ModuleFile.objects.filter(pk__in=[f.pk for f in files], forwarded_at__isnull=True).update(forwarded_at=now)
    Module.objects.filter(pk=module.pk).update(handoff_url_forwarded=url if url_pending else module.handoff_url_forwarded, handoff_sent_at=now)

    sendable = [f for f in files if (f.size or 0) <= MAX_FILE_BYTES][-MAX_FILES:]
    too_big = [f for f in files if (f.size or 0) > MAX_FILE_BYTES]
    owner = module.assignee.name if module.assignee_id else "The previous member"
    lines = [f'✅ {owner}\'s work on "{module.name}" ({module.project.name}) — approved by {approver.name}.']
    if url_pending:
        lines.append(f"🔗 Link: {url}")
    if sendable:
        lines.append(f"📎 {len(sendable)} file(s) attached below.")
    if too_big:
        lines.append("⚠️ Too large to forward here (download from the Projects page): " + ", ".join(f.original_name for f in too_big))
    lines.append(f'➡️ Please continue with your next task: "{next_module.name}".')
    _deliver(approver, recipient, request, text="\n".join(lines))
    for mf in sendable:
        try:
            data = _read_file(mf)
            _deliver(
                approver, recipient, request,
                content=ContentFile(data, name=mf.original_name),
                name=mf.original_name, size=len(data), mime=mf.mime_type,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Handoff: could not forward file %s", mf.pk)
    return True


def approve_handoff(module, admin_user, request):
    """Admin pressed "Approve & send". Returns (ok, message)."""
    next_module = find_next_module(module, module.assignee_id)
    if next_module is None:
        return False, "There is no next member to send this to."
    if not has_pending(module):
        return False, "Nothing new to send — everything was already sent to the next member."
    if not _forward_to_next(module, admin_user, next_module, request):
        return False, "Could not send to the next member."
    return True, f"Sent to {next_module.assignee.name}."


def handle_module_status_change(module, old_status, actor, request):
    """Call after a module's status really changed. Completing a module
    tells the admin (once for anything not announced yet); un-ticking just
    lets it be completed again - nothing is resent to the next member."""
    try:
        if module.status == ModuleStatusChoices.COMPLETED and old_status != ModuleStatusChoices.COMPLETED:
            if actor is None or actor.role == "admin":
                return
            module.refresh_from_db()
            files = list(module.files.filter(admin_notified_at__isnull=True).order_by("uploaded_at", "id"))
            if files:
                from .models import ModuleFile

                ModuleFile.objects.filter(pk__in=[f.pk for f in files]).update(admin_notified_at=timezone.now())
            url = (module.url or "").strip()
            link = url if url and url != (module.handoff_url_notified or "") else ""
            if link:
                Module.objects.filter(pk=module.pk).update(handoff_url_notified=link)
            _announce_to_admins(
                module, actor, request, files, link,
                f'✅ {actor.name} completed "{module.name}" ({module.project.name}).',
            )
    except Exception:  # noqa: BLE001
        logger.exception("Module hand-off failed for module %s", getattr(module, "pk", None))
