"""Per-request state, carried in a ContextVar.

Model signals (post_save / post_delete) have no idea which HTTP request — and
therefore which user — triggered them. The middleware stores a RequestState
here before the view runs, and the signal handlers read it back. A ContextVar
(not threading.local) is used because this project runs under ASGI (Daphne /
Channels), where a single thread can interleave requests.

RequestState is a plain mutable object on purpose: even if a ContextVar copy is
made when hopping between sync/async code, every copy still points at the SAME
state object, so counters set by signals are visible back in the middleware.
"""

import contextvars

_current = contextvars.ContextVar("reports_request_state", default=None)

_UNSET = object()


class RequestState:
    def __init__(self, request):
        self.request = request
        self._user = _UNSET
        self.logged = 0          # how many ActivityLog rows this request produced
        self.handled = 0         # tracked-model saves/deletes seen (even ones that changed nothing)
        self.created = set()     # {(model_label, pk)} created during this request

    @property
    def user(self):
        """The authenticated user behind this request, resolved lazily so
        read-only requests never pay for a lookup."""
        if self._user is _UNSET:
            self._user = self._resolve_user()
        return self._user

    def _resolve_user(self):
        req = self.request
        # DRF copies the user it authenticated onto the underlying Django
        # request, so by the time a view saves a model this is already set
        # (for both Token and Session auth) — no extra query.
        user = getattr(req, "user", None)
        if user is not None and getattr(user, "is_authenticated", False):
            return user

        # Fallback: resolve the DRF token ourselves (signals that fire before
        # DRF has authenticated, e.g. in other middleware).
        header = req.META.get("HTTP_AUTHORIZATION", "").split()
        if len(header) == 2 and header[0].lower() == "token":
            try:
                from rest_framework.authtoken.models import Token

                token = Token.objects.select_related("user").filter(key=header[1]).first()
                if token and token.user.is_active:
                    return token.user
            except Exception:  # never let logging break the request
                return None
        return None

    def set_user(self, user):
        self._user = user


def get_state():
    return _current.get()


def set_state(state):
    return _current.set(state)


def reset_state(token):
    _current.reset(token)
