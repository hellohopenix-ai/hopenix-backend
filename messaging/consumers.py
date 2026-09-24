import json

from channels.generic.websocket import AsyncJsonWebsocketConsumer


def user_group_name(user_id):
    """Every push for a given user goes to this one group name — used
    both here (to join it on connect) and in views.py (to send to it
    after a message is created), so the two stay in sync by construction."""
    return f"user_{user_id}"


# In-process count of currently-open websocket connections per user id
# (a user can have more than one tab/device open, hence a counter and
# not just a set). This only works correctly on a single process — see
# hopenix/settings.py's CHANNEL_LAYERS comment: as soon as REDIS_URL is
# set and this runs behind more than one worker, this dict stops being
# authoritative because each worker only sees ITS OWN connections. Good
# enough for this app's current single-process (Daphne, in-memory
# channel layer) setup, and used ONLY for "is this user reachable for a
# call right now" (messaging/views.py CallStartView) — never for
# message delivery, which still relies on the channel layer group push
# and therefore stays correct even across multiple workers.
_ONLINE_COUNTS = {}


def is_user_online(user_id):
    return _ONLINE_COUNTS.get(user_id, 0) > 0


def _mark_online(user_id):
    _ONLINE_COUNTS[user_id] = _ONLINE_COUNTS.get(user_id, 0) + 1


def _mark_offline(user_id):
    if user_id not in _ONLINE_COUNTS:
        return
    _ONLINE_COUNTS[user_id] -= 1
    if _ONLINE_COUNTS[user_id] <= 0:
        del _ONLINE_COUNTS[user_id]


class MessagingConsumer(AsyncJsonWebsocketConsumer):
    """One websocket connection per logged-in user (see routing.py).
    On connect: joins that user's personal group, so any event sent to
    `user_group_name(user.id)` from anywhere in the app (views.py, after
    SendMessageView creates a Message) is pushed to every tab/device
    that user currently has open.

    Frontend receives JSON events shaped like:
        {"type": "message.new", "message": {...same shape as MessageSerializer...}}
        {"type": "thread.read", "by_user_id": 5, "conversation_id": 12}
    """

    async def connect(self):
        self.user = self.scope.get("user")
        if not self.user or not self.user.is_authenticated:
            await self.close(code=4001)  # 4001 = custom "unauthenticated" close code
            return

        self.group_name = user_group_name(self.user.id)
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()
        _mark_online(self.user.id)

    async def disconnect(self, close_code):
        if getattr(self, "group_name", None):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)
        if getattr(self, "user", None) and self.user.is_authenticated:
            _mark_offline(self.user.id)

    async def receive_json(self, content, **kwargs):
        # Currently a one-way (server -> client) push channel — the
        # frontend still SENDS messages via the existing REST endpoint
        # (POST /api/messages/send/), not over the socket. A client-side
        # ping/pong to detect dead connections is the only inbound thing
        # we bother handling here.
        if content.get("type") == "ping":
            await self.send_json({"type": "pong"})

    # Called via channel_layer.group_send(group_name, {"type": "push.event", "payload": {...}})
    async def push_event(self, event):
        await self.send_json(event["payload"])