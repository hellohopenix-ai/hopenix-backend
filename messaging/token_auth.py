from urllib.parse import parse_qs

from channels.db import database_sync_to_async
from channels.middleware import BaseMiddleware
from django.contrib.auth.models import AnonymousUser


@database_sync_to_async
def get_user_from_ticket(ticket_key):
    from users.models import WsTicket  # imported here to avoid touching
                                        # models before django.setup()
    try:
        ticket = WsTicket.objects.select_related("user").get(key=ticket_key)
    except WsTicket.DoesNotExist:
        return AnonymousUser()

    if ticket.is_used or ticket.is_expired():
        return AnonymousUser()

    # Single-use: mark it consumed immediately so the same ticket can't
    # be replayed for a second connection even within its 30s window.
    ticket.is_used = True
    ticket.save(update_fields=["is_used"])
    return ticket.user


class TokenAuthMiddleware(BaseMiddleware):
    """Authenticates a websocket connection using a short-lived, single-use
    ticket (see users.views.IssueWsTicketView) rather than the long-lived
    DRF auth token itself.

    Browsers' native WebSocket() constructor can't set custom headers, so
    SOMETHING has to travel in the URL -- putting the real, long-lived
    token there means it can end up sitting in server/proxy access logs
    for as long as those logs are kept. A ticket is only ever good for
    one connection attempt within 30 seconds, so even if it leaks into a
    log line, it's already worthless by the time anyone could read it.

    Frontend flow:
        1. POST /api/auth/ws-ticket/  (normal fetch, Authorization: Token header)
        2. new WebSocket(`wss://.../ws/messages/?ticket=${ticket}`)
    """

    async def __call__(self, scope, receive, send):
        query_string = scope.get("query_string", b"").decode()
        ticket_key = parse_qs(query_string).get("ticket", [None])[0]

        scope["user"] = (
            await get_user_from_ticket(ticket_key) if ticket_key else AnonymousUser()
        )
        return await super().__call__(scope, receive, send)