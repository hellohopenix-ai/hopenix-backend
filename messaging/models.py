from django.conf import settings
from django.db import models


def message_upload_path(instance, filename):
    # messages/<conversation_id>/<filename> — mirrors the users app's
    # `cvs/` pattern (media/cvs/...), just scoped per-conversation so
    # attachments from different chats don't collide/overwrite.
    return f"messages/{instance.conversation_id}/{filename}"


class Conversation(models.Model):
    DIRECT = "direct"
    GROUP = "group"
    TYPE_CHOICES = [(DIRECT, "Direct"), (GROUP, "Group")]

    type = models.CharField(max_length=10, choices=TYPE_CHOICES, default=DIRECT)

    # Only used for GROUP conversations — a direct chat's display name is
    # just "the other participant's name", resolved per-viewer in the
    # serializer instead of stored here.
    name = models.CharField(max_length=255, blank=True)
    avatar = models.FileField(upload_to="conversation_avatars/", null=True, blank=True)

    participants = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        through="Participant",
        related_name="conversations",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    # Bumped every time a message is sent — lets the conversation list be
    # ordered "most recently active first", WhatsApp-style, with one
    # cheap `ORDER BY -updated_at` instead of a subquery per row.
    updated_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return self.name or f"Conversation #{self.pk}"

    def other_participant_for(self, user):
        """For a DIRECT conversation only: the participant that ISN'T
        `user`. Returns None for group conversations or if `user` isn't
        actually in it."""
        if self.type != self.DIRECT:
            return None
        return (
            self.participants.exclude(id=user.id).first()
            if user and user.is_authenticated
            else None
        )


class Participant(models.Model):
    """Join row between a Conversation and a User. Tracks read-state
    per-user, so unread counts/badges are correct for BOTH sides of a
    chat independently (exactly like the frontend's unread/unreadForUser
    split, but generalised to any number of participants — needed for
    groups, and for direct chats between two non-admin peers where
    neither side is "the" admin)."""

    conversation = models.ForeignKey(Conversation, related_name="memberships", on_delete=models.CASCADE)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, related_name="conversation_memberships", on_delete=models.CASCADE)
    unread_count = models.PositiveIntegerField(default=0)
    last_read_at = models.DateTimeField(null=True, blank=True)
    joined_at = models.DateTimeField(auto_now_add=True)
    # A soft "delete conversation" (only for me) hides it from that
    # user's inbox without destroying the thread for the other side —
    # matches the frontend's per-viewer delete behaviour. Re-appears
    # automatically the next time either side sends a new message.
    hidden = models.BooleanField(default=False)

    class Meta:
        unique_together = ("conversation", "user")

    def __str__(self):
        return f"{self.user.email} in {self.conversation_id}"


class Message(models.Model):
    TEXT = "text"
    FILE = "file"
    IMAGE = "image"
    VOICE = "voice"
    KIND_CHOICES = [(TEXT, "Text"), (FILE, "File"), (IMAGE, "Image"), (VOICE, "Voice")]

    conversation = models.ForeignKey(Conversation, related_name="messages", on_delete=models.CASCADE)
    sender = models.ForeignKey(settings.AUTH_USER_MODEL, related_name="sent_messages", on_delete=models.CASCADE)
    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, related_name="received_messages", null=True, blank=True, on_delete=models.CASCADE)

    kind = models.CharField(max_length=10, choices=KIND_CHOICES, default=TEXT)
    text = models.TextField(blank=True)

    # FILE/IMAGE messages
    attachment = models.FileField(upload_to=message_upload_path, null=True, blank=True)
    attachment_name = models.CharField(max_length=255, blank=True)
    attachment_size = models.PositiveIntegerField(null=True, blank=True)  # bytes

    # VOICE messages reuse `attachment` for the audio file itself.
    voice_duration_seconds = models.PositiveIntegerField(null=True, blank=True)

    is_read = models.BooleanField(default=False)
    is_deleted = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"Message #{self.pk} in conversation {self.conversation_id}"


class Call(models.Model):
    """A voice/video call between two users, signalled over the same
    per-user websocket group that messages already use (see
    consumers.py/views.py push_to_user) and connected peer-to-peer via
    WebRTC (the actual audio/video never touches this server — only the
    SDP offer/answer + ICE candidates are relayed through it).

    Whether the callee's phone even *rings* depends on whether they have
    a live websocket connection right now (see consumers.is_user_online)
    — NOT the slower 2-minute presence_status() window messaging already
    uses elsewhere, since a call needs to know "are they reachable RIGHT
    NOW", not "were they active recently". If they aren't connected, the
    call is recorded MISSED immediately instead of ringing into the void.
    """

    AUDIO = "audio"
    VIDEO = "video"
    TYPE_CHOICES = [(AUDIO, "Audio"), (VIDEO, "Video")]

    RINGING = "ringing"
    ONGOING = "ongoing"
    ENDED = "ended"
    MISSED = "missed"
    REJECTED = "rejected"
    STATUS_CHOICES = [
        (RINGING, "Ringing"),
        (ONGOING, "Ongoing"),
        (ENDED, "Ended"),
        (MISSED, "Missed"),
        (REJECTED, "Rejected"),
    ]

    conversation = models.ForeignKey(Conversation, related_name="calls", null=True, on_delete=models.SET_NULL)
    caller = models.ForeignKey(settings.AUTH_USER_MODEL, related_name="calls_made", on_delete=models.CASCADE)
    callee = models.ForeignKey(settings.AUTH_USER_MODEL, related_name="calls_received", on_delete=models.CASCADE)

    call_type = models.CharField(max_length=10, choices=TYPE_CHOICES, default=AUDIO)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=RINGING)

    started_at = models.DateTimeField(auto_now_add=True)
    answered_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    duration_seconds = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"Call #{self.pk} {self.caller_id} -> {self.callee_id} ({self.status})"


class PushSubscription(models.Model):
    """One row per browser/device the user has granted Notification
    permission on and subscribed to via the Push API (see the frontend's
    pushSubscription.js). Used ONLY to wake the browser with an OS-level
    push notification when they're NOT connected to the per-user
    websocket (see consumers.is_user_online) — e.g. the Hopenix tab or
    browser is fully closed — so an incoming call can still reach them
    the way it would if the tab were open (see push_utils.py and
    CallStartView). A user can have several rows (phone + laptop, or two
    browsers)."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, related_name="push_subscriptions", on_delete=models.CASCADE)
    endpoint = models.URLField(max_length=500, unique=True)
    p256dh = models.CharField(max_length=255)
    auth = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"PushSubscription for {self.user.email} ({self.endpoint[:40]}...)"