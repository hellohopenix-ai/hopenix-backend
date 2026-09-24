import logging
import re

from django.conf import settings

from .constants import DEFAULT_IGNORED_PATHS, PATH_TO_MODULE
from .context import RequestState, reset_state, set_state
from .services import log_activity

logger = logging.getLogger(__name__)

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _ignored_patterns():
    extra = getattr(settings, "ACTIVITY_LOG_IGNORE_PATHS", ())
    return [re.compile(p) for p in (*DEFAULT_IGNORED_PATHS, *extra)]


class ActivityContextMiddleware:
    """Two jobs:

    1. Put the current request in a ContextVar, so the model signals in
       reports/signals.py can attribute a save/delete to the right user.
    2. Safety net: an authenticated API write that didn't touch any tracked
       model (so signals recorded nothing) still gets ONE generic row, and
       file downloads are recorded too. Nothing a user does through the API
       is silently missed. Noisy endpoints are skipped — see
       DEFAULT_IGNORED_PATHS in constants.py and the optional
       ACTIVITY_LOG_IGNORE_PATHS setting.

    Place it AFTER AuthenticationMiddleware in settings.MIDDLEWARE.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self.ignored = _ignored_patterns()

    def __call__(self, request):
        state = RequestState(request)
        token = set_state(state)
        try:
            response = self.get_response(request)
        finally:
            reset_state(token)

        try:
            self._fallback_log(request, response, state)
        except Exception:
            logger.exception("Fallback activity logging failed")
        return response

    def _fallback_log(self, request, response, state):
        path = request.path
        if not path.startswith("/api/") or state.logged or state.handled:
            return
        status = getattr(response, "status_code", 0)
        if status >= 400:
            return

        is_write = request.method in WRITE_METHODS
        is_download = request.method == "GET" and "download" in path and status == 200
        if not (is_write or is_download):
            return
        if any(p.search(path) for p in self.ignored):
            return

        user = state.user
        if user is None:  # public/anonymous endpoints (intake form, OTP, ...) are not "user activity"
            return

        segment = path[len("/api/"):].split("/", 1)[0]
        module = PATH_TO_MODULE.get(segment, "General")
        action = "download" if is_download else "api"
        verb = {"POST": "Performed", "PUT": "Updated", "PATCH": "Updated", "DELETE": "Deleted"}.get(request.method, "Downloaded")
        # Re-enter the ContextVar so log_activity() can read the request.
        token = set_state(state)
        try:
            log_activity(
                action=action,
                user=user,
                module=module,
                description=f"{verb} via {request.method} {path}",
                status_code=status,
            )
        finally:
            reset_state(token)
