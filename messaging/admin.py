from django.contrib import admin
from .models import Call, Conversation, Participant, Message

@admin.register(Conversation)
class ConversationAdmin(admin.ModelAdmin):
    list_display = ("id", "type", "name", "created_by", "created_at", "updated_at")

@admin.register(Participant)
class ParticipantAdmin(admin.ModelAdmin):
    list_display = ("id", "conversation", "user", "unread_count", "hidden", "joined_at")

@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ("id", "conversation", "sender", "recipient", "kind", "text", "is_read", "created_at")

@admin.register(Call)
class CallAdmin(admin.ModelAdmin):
    list_display = ("id", "caller", "callee", "call_type", "status", "started_at", "duration_seconds")