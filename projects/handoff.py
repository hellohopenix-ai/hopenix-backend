"""Hand-off between the members of a project, WITH admin approval.

Employees are not allowed to message each other, so the work never goes
straight from one member to the next:

1. A module is marked Completed (Tasks page or Projects page).
   -> its link / files / zips are sent to the ADMIN(s) as a normal chat
      message, with a note saying who the next member is.
2. An admin approves ("Approve & send" on the module, Projects page).
   -> the same link / files / zips, plus "please continue with <next
      module>", are sent from the admin to the next member.
3. When that member finishes, the cycle repeats towards the one after them.

If an ADMIN is the one who ticks the module, that counts as the approval and
step 2 happens immediately. Un-ticking a module resets everything, so ticking
it again starts a fresh cycle. Never allowed to fail the tick itself.
"""
import logging
from datetime import timedelta

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


def _send_bundle(sender, recipient, request, module, intro_lines, closing_line, skip_recent_dupes):
    """One text message (intro + link + file count + closing) followed by the
    module's files, newest MAX_FILES. `skip_recent_dupes`: don't re-send a
    file this same sender already sent this same recipient in the last 10
    minutes (the Tasks page already forwards completion files to the admin
    itself - this keeps the admin from getting every zip twice)."""
    files = list(module.files.order_by("-uploaded_at", "-id")[:MAX_FILES])
    files.reverse()
    if skip_recent_dupes and files:
        from messaging.models import Message

        since = timezone.now() - timedelta(minutes=10)
        already = set(
            Message.objects.filter(
                sender=sender, recipient=recipient, created_at__gte=since,
                attachment_name__in=[f.original_name for f in files],
            ).values_list("attachment_name", flat=True)
        )
        files = [f for f in files if f.original_name not in already]
    sendable = [f for f in files if (f.size or 0) <= MAX_FILE_BYTES]
    too_big = [f for f in files if (f.size or 0) > MAX_FILE_BYTES]

    lines = list(intro_lines)
    if module.url:
        lines.append(f"🔗 Link: {module.url}")
    if sendable:
        lines.append(f"📎 {len(sendable)} file(s) attached below.")
    if too_big:
        lines.append("⚠️ Too large to forward here (download from the Projects page): " + ", ".join(f.original_name for f in too_big))
    lines.append(closing_line)
    _deliver(sender, recipient, request, text="\n".join(lines))

    for mf in sendable:
        try:
            mf.file.open("rb")
            try:
                data = mf.file.read()
            finally:
                mf.file.close()
            _deliver(
                sender, recipient, request,
                content=ContentFile(data, name=mf.original_name),
                name=mf.original_name, size=len(data), mime=mf.mime_type,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Handoff: could not forward file %s", mf.pk)


def _forward_to_next(module, approver, next_module, request):
    """Step 2: approver (an admin) -> next member."""
    recipient = next_module.assignee
    if recipient is None or recipient.id == approver.id:
        return False
    flipped = Module.objects.filter(pk=module.pk, handoff_sent_at__isnull=True).update(
        handoff_sent_at=timezone.now()
    )
    if not flipped:
        return False
    owner = module.assignee.name if module.assignee_id else "The previous member"
    _send_bundle(
        approver, recipient, request, module,
        intro_lines=[f'✅ {owner} completed "{module.name}" ({module.project.name}) — approved by {approver.name}.'],
        closing_line=f'➡️ Please continue with your next task: "{next_module.name}".',
        skip_recent_dupes=False,
    )
    return True


def request_handoff(module, actor, request):
    """Step 1: module just got completed."""
    ref_id = module.assignee_id or getattr(actor, "id", None)
    next_module = find_next_module(module, ref_id)
    if next_module is None:
        return

    # An admin ticking the module IS the approval.
    if actor.role == "admin":
        Module.objects.filter(pk=module.pk, handoff_requested_at__isnull=True).update(
            handoff_requested_at=timezone.now()
        )
        _forward_to_next(module, actor, next_module, request)
        return

    flipped = Module.objects.filter(pk=module.pk, handoff_requested_at__isnull=True).update(
        handoff_requested_at=timezone.now()
    )
    if not flipped:
        return
    User = get_user_model()
    admins = User.objects.filter(role="admin", status="approved", is_active=True).exclude(id=actor.id)
    for admin in admins:
        try:
            _send_bundle(
                actor, admin, request, module,
                intro_lines=[f'✅ {actor.name} completed "{module.name}" ({module.project.name}).'],
                closing_line=(
                    f'⏳ Waiting for your approval to send this to {next_module.assignee.name} '
                    f'for "{next_module.name}". Approve it from Projects → the module → "Approve & send".'
                ),
                skip_recent_dupes=True,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Handoff: request to admin %s failed", admin.id)


def approve_handoff(module, admin_user, request):
    """Admin approval. Returns (ok, message)."""
    if module.status != ModuleStatusChoices.COMPLETED:
        return False, "This module is not completed."
    if module.handoff_sent_at:
        return False, "Already sent to the next member."
    next_module = find_next_module(module, module.assignee_id)
    if next_module is None:
        return False, "There is no next member to send this to."
    if not Module.objects.filter(pk=module.pk, handoff_requested_at__isnull=True).update(
        handoff_requested_at=timezone.now()
    ):
        pass  # already requested - fine
    if not _forward_to_next(module, admin_user, next_module, request):
        return False, "Could not send to the next member."
    return True, f"Sent to {next_module.assignee.name}."


def handle_module_status_change(module, old_status, actor, request):
    """Call after a module's status really changed."""
    try:
        if module.status == ModuleStatusChoices.COMPLETED and old_status != ModuleStatusChoices.COMPLETED:
            request_handoff(module, actor, request)
        elif module.status != ModuleStatusChoices.COMPLETED and old_status == ModuleStatusChoices.COMPLETED:
            # un-ticked: ticking it again later starts a fresh cycle
            Module.objects.filter(pk=module.pk).update(handoff_requested_at=None, handoff_sent_at=None)
    except Exception:  # noqa: BLE001
        logger.exception("Module hand-off failed for module %s", getattr(module, "pk", None))
