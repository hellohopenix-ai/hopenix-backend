from django.contrib.auth import get_user_model
from django.db import models
from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.parsers import FormParser, MultiPartParser, JSONParser
from rest_framework.response import Response
from rest_framework.views import APIView

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

from .consumers import is_user_online, user_group_name
from .models import Call, Conversation, Message, Participant, PushSubscription
from .permissions import can_message, get_allowed_contact_ids, get_allowed_contacts
from .push_utils import send_incoming_call_push
from .serializers import CallSerializer, ContactSerializer, ConversationListSerializer, MessageSerializer

User = get_user_model()


def push_to_user(user_id, payload):
    """Send a JSON payload to every open websocket tab/device that
    `user_id` currently has connected (see messaging/consumers.py). If
    they aren't connected right now, this is a harmless no-op — the
    REST polling in MessagesPage.jsx is still the fallback, so nothing
    is ever actually lost, just delivered a bit slower."""
    channel_layer = get_channel_layer()
    if channel_layer is None:
        return
    async_to_sync(channel_layer.group_send)(
        user_group_name(user_id),
        {"type": "push.event", "payload": payload},
    )


def get_or_create_direct_conversation(user1, user2):
    """Finds or creates a direct 1-to-1 conversation between user1 and user2."""
    conv = (
        Conversation.objects.filter(type=Conversation.DIRECT, memberships__user=user1)
        .filter(memberships__user=user2)
        .first()
    )
    if not conv:
        conv = Conversation.objects.create(type=Conversation.DIRECT, created_by=user1)
        Participant.objects.create(conversation=conv, user=user1)
        Participant.objects.create(conversation=conv, user=user2)
    return conv


class ConversationsListView(APIView):
    """GET /api/messages/conversations/
    Returns all conversations for the logged-in user, ordered by last activity."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        request.user.touch_active()
        user = request.user

        # Ensure a conversation exists for every contact this user is
        # actually ALLOWED to see (admin -> everyone approved; anyone
        # else -> just Admin + their own assigned manager). This is the
        # role-based visibility rule — see messaging/permissions.py.
        allowed_ids = get_allowed_contact_ids(user)
        for other in get_allowed_contacts(user):
            get_or_create_direct_conversation(user, other)

        convs = (
            Conversation.objects.filter(memberships__user=user, memberships__hidden=False)
            .distinct()
            .order_by("-updated_at")
        )
        # A DIRECT conversation stays in the database even after the other
        # person stops being an allowed contact (e.g. a manager gets
        # unassigned) — that's intentional, so history isn't lost if they're
        # ever reassigned. But it shouldn't keep cluttering the sidebar (and
        # 403ing when opened) in the meantime, so filter those out here.
        # GROUP conversations aren't subject to this rule.
        convs = [
            c for c in convs
            if c.type != Conversation.DIRECT
            or (lambda other: other and other.id in allowed_ids)(c.other_participant_for(user))
        ]
        return Response(ConversationListSerializer(convs, many=True, context={"request": request}).data)


class ThreadMessagesView(APIView):
    """GET /api/messages/thread/<user_id>/
    Returns all messages between request.user and <user_id>."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, user_id):
        request.user.touch_active()
        try:
            target_user = User.objects.get(id=user_id)
        except User.DoesNotExist:
            return Response({"error": "User not found."}, status=status.HTTP_404_NOT_FOUND)

        if not can_message(request.user, target_user):
            return Response({"error": "You are not allowed to message this user."}, status=status.HTTP_403_FORBIDDEN)

        conv = get_or_create_direct_conversation(request.user, target_user)
        messages = conv.messages.filter(is_deleted=False).order_by("created_at")
        return Response(MessageSerializer(messages, many=True, context={"request": request}).data)


class SendMessageView(APIView):
    """POST /api/messages/send/
    Body: { recipient: <user_id>, text: string, attachment?: file, kind?: string }"""

    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def post(self, request):
        request.user.touch_active()
        data = request.data
        recipient_id = data.get("recipient") or data.get("recipientId") or data.get("recipient_id")
        text = (data.get("text") or "").strip()
        attachment = request.FILES.get("attachment")
        kind = data.get("kind") or "text"

        if not recipient_id:
            return Response({"error": "recipient is required."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            recipient = User.objects.get(id=recipient_id)
        except User.DoesNotExist:
            return Response({"error": "Recipient user not found."}, status=status.HTTP_404_NOT_FOUND)

        if not can_message(request.user, recipient):
            return Response({"error": "You are not allowed to message this user."}, status=status.HTTP_403_FORBIDDEN)

        if not text and not attachment:
            return Response({"error": "Message text or attachment is required."}, status=status.HTTP_400_BAD_REQUEST)

        if attachment and kind == "text":
            content_type = getattr(attachment, "content_type", "")
            if content_type.startswith("image/"):
                kind = "image"
            elif content_type.startswith("audio/"):
                kind = "voice"
            else:
                kind = "file"

        conv = get_or_create_direct_conversation(request.user, recipient)
        now = timezone.now()

        msg = Message.objects.create(
            conversation=conv,
            sender=request.user,
            recipient=recipient,
            kind=kind,
            text=text,
            attachment=attachment if attachment else None,
            attachment_name=attachment.name if attachment else "",
            attachment_size=attachment.size if attachment else None,
        )

        conv.updated_at = now
        conv.save(update_fields=["updated_at"])

        # Unhide and increment unread count for recipient participant
        Participant.objects.filter(conversation=conv, user=recipient).update(
            hidden=False, unread_count=models.F("unread_count") + 1
        )
        # Unhide for sender as well
        Participant.objects.filter(conversation=conv, user=request.user).update(hidden=False)

        message_data = MessageSerializer(msg, context={"request": request}).data
        # Push a lightweight "something changed" signal to both sides —
        # NOT the fully-serialized conversation, because fields like
        # unreadCount are per-viewer (ConversationListSerializer reads
        # request.user from context), so one snapshot can't be correct
        # for both people at once. The frontend just re-runs its normal
        # fetchConversations()/fetchThread() REST calls on receiving
        # this, reusing logic that's already correct per-viewer — REST
        # polling in MessagesPage.jsx keeps working underneath too, as
        # a fallback in case a socket is down.
        push_to_user(recipient.id, {
            "type": "message.new", "conversation_id": conv.id,
            "sender_id": request.user.id, "recipient_id": recipient.id,
            "message": message_data,
        })
        push_to_user(request.user.id, {
            "type": "message.new", "conversation_id": conv.id,
            "sender_id": request.user.id, "recipient_id": recipient.id,
            "message": message_data,
        })

        return Response(message_data, status=status.HTTP_201_CREATED)


class MarkThreadReadView(APIView):
    """POST /api/messages/thread/<user_id>/read/
    Marks all messages from <user_id> to request.user as read."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, user_id):
        request.user.touch_active()
        try:
            target_user = User.objects.get(id=user_id)
        except User.DoesNotExist:
            return Response({"error": "User not found."}, status=status.HTTP_404_NOT_FOUND)

        conv = get_or_create_direct_conversation(request.user, target_user)
        Message.objects.filter(conversation=conv, sender=target_user, recipient=request.user, is_read=False).update(
            is_read=True
        )

        Participant.objects.filter(conversation=conv, user=request.user).update(
            unread_count=0, last_read_at=timezone.now()
        )
        push_to_user(target_user.id, {
            "type": "thread.read", "conversation_id": conv.id, "read_by_user_id": request.user.id,
        })
        return Response({"success": True})


class ContactsView(APIView):
    """GET /api/messages/contacts/"""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        contacts = get_allowed_contacts(request.user)
        return Response(ContactSerializer(contacts, many=True, context={"request": request}).data)

class CallStartView(APIView):
    """POST /api/messages/calls/start/
    Body: { calleeId: <user_id>, callType?: "audio"|"video" }

    If the callee currently has a live websocket connection (see
    messaging/consumers.is_user_online — real-time, NOT the slower
    "active in the last 2 minutes" presence_status used elsewhere), a
    "call.incoming" event is pushed to them and the call starts RINGING.
    Otherwise there's nobody to ring — the call is recorded MISSED
    immediately and nothing is pushed live (they'll see it the next
    time they open Messages, same as any REST-polled data)."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        request.user.touch_active()
        callee_id = request.data.get("calleeId") or request.data.get("callee_id")
        call_type = request.data.get("callType") or request.data.get("call_type") or Call.AUDIO
        if call_type not in (Call.AUDIO, Call.VIDEO):
            call_type = Call.AUDIO

        if not callee_id:
            return Response({"error": "calleeId is required."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            callee = User.objects.get(id=callee_id)
        except User.DoesNotExist:
            return Response({"error": "User not found."}, status=status.HTTP_404_NOT_FOUND)

        if not can_message(request.user, callee):
            return Response({"error": "You are not allowed to call this user."}, status=status.HTTP_403_FORBIDDEN)

        # Don't let either side start a second call while one is already
        # in flight between the same two people (ringing or ongoing).
        existing = Call.objects.filter(
            status__in=[Call.RINGING, Call.ONGOING]
        ).filter(
            models.Q(caller=request.user, callee=callee) | models.Q(caller=callee, callee=request.user)
        ).first()
        if existing:
            return Response(CallSerializer(existing, context={"request": request}).data, status=status.HTTP_200_OK)

        conv = get_or_create_direct_conversation(request.user, callee)
        callee_reachable = is_user_online(callee.id)
        # Can't reach them over a live socket right now, but they've
        # granted notification permission on at least one device before
        # (see PushSubscribeView) — an OS-level push can still ring them
        # even with the tab/browser fully closed.
        has_push = PushSubscription.objects.filter(user=callee).exists()
        reachable = callee_reachable or has_push

        call = Call.objects.create(
            conversation=conv,
            caller=request.user,
            callee=callee,
            call_type=call_type,
            status=Call.RINGING if reachable else Call.MISSED,
            ended_at=None if reachable else timezone.now(),
        )

        call_data = CallSerializer(call, context={"request": request}).data

        if callee_reachable:
            push_to_user(callee.id, {"type": "call.incoming", "call": call_data})
        elif has_push:
            # Call stays RINGING (not MISSED) even with no live tab — it's
            # up to the caller's own ring-timeout to give up and call
            # CallEndView if the callee never opens the notification (see
            # callsApi.js / the frontend's ring-timeout, same as it
            # already does for an online-but-unanswered call).
            send_incoming_call_push(callee, call_data, request.user.name)
        else:
            # Neither a live tab nor a push subscription — nothing can
            # possibly reach them, so record it missed immediately.
            pass

        return Response(call_data, status=status.HTTP_201_CREATED)


class CallRespondView(APIView):
    """POST /api/messages/calls/<id>/respond/
    Body: { action: "accept" | "reject" } — only the callee may respond."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, call_id):
        request.user.touch_active()
        try:
            call = Call.objects.get(id=call_id)
        except Call.DoesNotExist:
            return Response({"error": "Call not found."}, status=status.HTTP_404_NOT_FOUND)

        if call.callee_id != request.user.id:
            return Response({"error": "Only the callee can respond to this call."}, status=status.HTTP_403_FORBIDDEN)
        if call.status != Call.RINGING:
            return Response(CallSerializer(call, context={"request": request}).data, status=status.HTTP_200_OK)

        action = request.data.get("action")
        now = timezone.now()
        if action == "accept":
            call.status = Call.ONGOING
            call.answered_at = now
            call.save(update_fields=["status", "answered_at"])
            push_to_user(call.caller_id, {"type": "call.accepted", "call": CallSerializer(call, context={"request": request}).data})
        elif action == "reject":
            call.status = Call.REJECTED
            call.ended_at = now
            call.save(update_fields=["status", "ended_at"])
            push_to_user(call.caller_id, {"type": "call.rejected", "call": CallSerializer(call, context={"request": request}).data})
        else:
            return Response({"error": "action must be 'accept' or 'reject'."}, status=status.HTTP_400_BAD_REQUEST)

        return Response(CallSerializer(call, context={"request": request}).data)


class CallEndView(APIView):
    """POST /api/messages/calls/<id>/end/
    Body: { reason?: "hangup" | "no_answer" } — either party may end/cancel
    a call. If it never got past RINGING, ending it means "missed" (either
    the caller cancelled before the callee answered, or the callee simply
    never picked up in time — the frontend decides which via its own
    ring timeout and either way it's recorded the same: MISSED)."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, call_id):
        request.user.touch_active()
        try:
            call = Call.objects.get(id=call_id)
        except Call.DoesNotExist:
            return Response({"error": "Call not found."}, status=status.HTTP_404_NOT_FOUND)

        if request.user.id not in (call.caller_id, call.callee_id):
            return Response({"error": "You are not part of this call."}, status=status.HTTP_403_FORBIDDEN)
        if call.status in (Call.ENDED, Call.MISSED, Call.REJECTED):
            return Response(CallSerializer(call, context={"request": request}).data)

        now = timezone.now()
        if call.status == Call.RINGING:
            call.status = Call.MISSED
        else:
            call.status = Call.ENDED
            if call.answered_at:
                call.duration_seconds = max(0, int((now - call.answered_at).total_seconds()))
        call.ended_at = now
        call.save(update_fields=["status", "ended_at", "duration_seconds"])

        call_data = CallSerializer(call, context={"request": request}).data
        other_id = call.callee_id if request.user.id == call.caller_id else call.caller_id
        push_to_user(other_id, {"type": "call.ended", "call": call_data})

        return Response(call_data)


class CallSignalView(APIView):
    """POST /api/messages/calls/<id>/signal/
    Body: { toUserId, data } — relays a WebRTC SDP offer/answer or ICE
    candidate to the other participant over their existing websocket
    (same push_to_user channel messages already use). The actual audio/
    video stream is peer-to-peer between the two browsers; only this
    small handshake payload ever touches the server."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, call_id):
        try:
            call = Call.objects.get(id=call_id)
        except Call.DoesNotExist:
            return Response({"error": "Call not found."}, status=status.HTTP_404_NOT_FOUND)

        if request.user.id not in (call.caller_id, call.callee_id):
            return Response({"error": "You are not part of this call."}, status=status.HTTP_403_FORBIDDEN)

        to_user_id = call.callee_id if request.user.id == call.caller_id else call.caller_id
        push_to_user(to_user_id, {
            "type": "call.signal", "call_id": call.id,
            "from_user_id": request.user.id, "data": request.data.get("data"),
        })
        return Response({"success": True})


class CallHistoryView(APIView):
    """GET /api/messages/calls/thread/<user_id>/
    Recent calls between request.user and <user_id> — same idea as
    ThreadMessagesView but for calls, so the Messages page can show a
    "Missed call" / "Call ended · 3:12" strip for the open conversation."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, user_id):
        try:
            other = User.objects.get(id=user_id)
        except User.DoesNotExist:
            return Response({"error": "User not found."}, status=status.HTTP_404_NOT_FOUND)

        calls = Call.objects.filter(
            models.Q(caller=request.user, callee=other) | models.Q(caller=other, callee=request.user)
        ).order_by("-started_at")[:20]
        return Response(CallSerializer(calls, many=True, context={"request": request}).data)


class ActiveIncomingCallView(APIView):
    """GET /api/messages/calls/active/
    Returns the current RINGING call where request.user is the callee, or
    null. Lets the frontend recover an in-progress ring on page load —
    e.g. the user opened the app from a push notification (see push_utils.py)
    after missing the live "call.incoming" websocket event because their
    tab/browser was closed when it was sent."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        call = Call.objects.filter(callee=request.user, status=Call.RINGING).order_by("-started_at").first()
        if not call:
            return Response(None)
        return Response(CallSerializer(call, context={"request": request}).data)


class PushSubscribeView(APIView):
    """POST /api/messages/push/subscribe/
    Body: the raw PushSubscription object the frontend gets back from
    `pushManager.subscribe(...)`, as JSON (subscription.toJSON()) — i.e.
    { endpoint, keys: { p256dh, auth } }. Upserts by endpoint, so
    re-subscribing (e.g. after clearing site data) never creates
    duplicate rows for the same browser."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        endpoint = request.data.get("endpoint")
        keys = request.data.get("keys") or {}
        p256dh = keys.get("p256dh")
        auth = keys.get("auth")
        if not endpoint or not p256dh or not auth:
            return Response(
                {"error": "endpoint and keys.p256dh/keys.auth are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        PushSubscription.objects.update_or_create(
            endpoint=endpoint,
            defaults={"user": request.user, "p256dh": p256dh, "auth": auth},
        )
        return Response({"success": True}, status=status.HTTP_201_CREATED)


class PushUnsubscribeView(APIView):
    """POST /api/messages/push/unsubscribe/   Body: { endpoint }"""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        endpoint = request.data.get("endpoint")
        if endpoint:
            PushSubscription.objects.filter(endpoint=endpoint, user=request.user).delete()
        return Response({"success": True})