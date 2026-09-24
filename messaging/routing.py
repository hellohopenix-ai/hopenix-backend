from django.urls import re_path

from . import consumers

websocket_urlpatterns = [
    # One socket per logged-in user (not per-conversation) — it just
    # joins that user's own personal group and receives every event
    # relevant to them (new message in ANY of their conversations,
    # read receipts, etc). Simpler than a socket-per-thread and matches
    # how the frontend already has ONE user logged in at a time.
    re_path(r"^ws/messages/$", consumers.MessagingConsumer.as_asgi()),
]
