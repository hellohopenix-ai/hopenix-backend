from django.contrib.auth import get_user_model
from rest_framework import serializers

from .models import Call, Conversation, Message, Participant

User = get_user_model()


def resolve_avatar_url(user, request):
    """Same priority ContactSerializer.get_avatar already used: the
    self-editable User.avatar first, profile.profile_photo (the one-time
    registration photo) as fallback. Pulled out as a standalone function
    so CallSerializer can use the exact same logic for callerAvatar/
    calleeAvatar — CallSerializer has no `request.user`-is-the-viewer
    concept the way ContactSerializer's get_avatar implicitly assumed
    nothing extra from context, so it's safe to reuse as-is."""
    profile = getattr(user, "profile", None)
    source = user.avatar or (profile.profile_photo if profile else None)
    if not source:
        return ""
    url = source.url
    full_url = request.build_absolute_uri(url) if request else url
    if profile and profile.updated_at:
        ts = int(profile.updated_at.timestamp())
        return f"{full_url}?v={ts}"
    return full_url


class ContactSerializer(serializers.ModelSerializer):
    """A person the current viewer is allowed to see/message — used by
    GET /api/messages/contacts/ and to describe the "other side" of a
    direct conversation."""

    status = serializers.SerializerMethodField()
    avatar = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ["id", "name", "email", "role", "department", "status", "avatar"]

    def get_status(self, obj):
        return obj.presence_status()

    def get_avatar(self, obj):
        request = self.context.get("request")
        return resolve_avatar_url(obj, request)


class MessageSerializer(serializers.ModelSerializer):
    senderId = serializers.IntegerField(source="sender_id", read_only=True)
    senderName = serializers.CharField(source="sender.name", read_only=True)
    recipientId = serializers.IntegerField(source="recipient_id", read_only=True, default=None)
    recipientName = serializers.CharField(source="recipient.name", read_only=True, default=None)
    isMine = serializers.SerializerMethodField()
    attachmentUrl = serializers.SerializerMethodField()
    attachmentName = serializers.CharField(source="attachment_name", read_only=True)
    attachmentSize = serializers.IntegerField(source="attachment_size", read_only=True)
    isImage = serializers.BooleanField(source="kind", read_only=True)  # overridden below
    voiceDuration = serializers.IntegerField(source="voice_duration_seconds", read_only=True)
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)
    is_read = serializers.BooleanField(read_only=True)

    class Meta:
        model = Message
        fields = [
            "id", "conversation_id", "senderId", "senderName", "recipientId", "recipientName",
            "kind", "text", "attachmentUrl", "attachmentName", "attachmentSize", "isImage",
            "voiceDuration", "createdAt", "isMine", "is_read", "is_deleted",
        ]

    def get_isMine(self, obj):
        request = self.context.get("request")
        return bool(request and obj.sender_id == request.user.id)

    def get_attachmentUrl(self, obj):
        if not obj.attachment:
            return None
        request = self.context.get("request")
        url = obj.attachment.url
        return request.build_absolute_uri(url) if request else url

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data["isImage"] = instance.kind == Message.IMAGE
        if instance.is_deleted:
            data["text"] = ""
            data["attachmentUrl"] = None
        return data


class ConversationListSerializer(serializers.ModelSerializer):
    """One row per conversation for the current viewer's inbox list."""

    isGroup = serializers.SerializerMethodField()
    name = serializers.SerializerMethodField()
    avatar = serializers.SerializerMethodField()
    otherUser = serializers.SerializerMethodField()
    members = serializers.SerializerMethodField()
    lastMessage = serializers.SerializerMethodField()
    unreadCount = serializers.SerializerMethodField()
    updatedAt = serializers.DateTimeField(source="updated_at", read_only=True)

    class Meta:
        model = Conversation
        fields = ["id", "isGroup", "name", "avatar", "otherUser", "members", "lastMessage", "unreadCount", "updatedAt"]

    def _viewer(self):
        return self.context["request"].user

    def get_isGroup(self, obj):
        return obj.type == Conversation.GROUP

    def get_name(self, obj):
        if obj.type == Conversation.GROUP:
            return obj.name
        other = obj.other_participant_for(self._viewer())
        return other.name if other else ""

    def get_avatar(self, obj):
        if obj.avatar:
            request = self.context.get("request")
            return request.build_absolute_uri(obj.avatar.url) if request else obj.avatar.url
        return ""

    def get_otherUser(self, obj):
        if obj.type != Conversation.DIRECT:
            return None
        other = obj.other_participant_for(self._viewer())
        return ContactSerializer(other, context=self.context).data if other else None

    def get_members(self, obj):
        if obj.type != Conversation.GROUP:
            return None
        return list(obj.participants.values_list("name", flat=True))

    def get_lastMessage(self, obj):
        last = obj.messages.order_by("-created_at").first()
        if not last:
            return None
        return {
            "text": "" if last.is_deleted else last.text,
            "kind": last.kind,
            "senderId": last.sender_id,
            "createdAt": last.created_at,
        }

    def get_unreadCount(self, obj):
        membership = obj.memberships.filter(user=self._viewer()).first()
        return membership.unread_count if membership else 0

class CallSerializer(serializers.ModelSerializer):
    callerId = serializers.IntegerField(source="caller_id", read_only=True)
    callerName = serializers.CharField(source="caller.name", read_only=True)
    callerAvatar = serializers.SerializerMethodField()
    calleeId = serializers.IntegerField(source="callee_id", read_only=True)
    calleeName = serializers.CharField(source="callee.name", read_only=True)
    calleeAvatar = serializers.SerializerMethodField()
    callType = serializers.CharField(source="call_type", read_only=True)
    startedAt = serializers.DateTimeField(source="started_at", read_only=True)
    answeredAt = serializers.DateTimeField(source="answered_at", read_only=True)
    endedAt = serializers.DateTimeField(source="ended_at", read_only=True)
    durationSeconds = serializers.IntegerField(source="duration_seconds", read_only=True)
    isMine = serializers.SerializerMethodField()

    class Meta:
        model = Call
        fields = [
            "id", "conversation_id", "callerId", "callerName", "callerAvatar",
            "calleeId", "calleeName", "calleeAvatar",
            "callType", "status", "startedAt", "answeredAt", "endedAt", "durationSeconds", "isMine",
        ]

    def get_isMine(self, obj):
        request = self.context.get("request")
        return bool(request and obj.caller_id == request.user.id)

    def get_callerAvatar(self, obj):
        return resolve_avatar_url(obj.caller, self.context.get("request"))

    def get_calleeAvatar(self, obj):
        return resolve_avatar_url(obj.callee, self.context.get("request"))