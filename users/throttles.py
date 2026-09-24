"""
Rate limiting for the authentication endpoints.

Two kinds are combined on every sensitive view below:

  * IP-based (ScopedRateThrottle) — stops one machine from hammering the
    endpoint, whoever it's targeting. Rates are set in settings.py under
    REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"].

  * Email-based (EmailKeyedThrottle) — stops an attacker who spreads the
    SAME attack across many IPs/proxies from still brute-forcing one
    specific account, since it keys off the email in the request body
    instead of the caller's address.

Both use Django's cache framework, so they work out of the box with the
default local-memory cache. If you run more than one server process
(gunicorn workers, multiple containers, etc.), point CACHES at Redis
(you already have REDIS_URL configured for Channels) so all processes
share the same counters — otherwise an attacker can dodge the limit by
landing on a different worker each time.
"""

from rest_framework.throttling import ScopedRateThrottle, SimpleRateThrottle


class LoginRateThrottle(ScopedRateThrottle):
    scope = "login"


class RegisterRateThrottle(ScopedRateThrottle):
    scope = "register"


class OtpRateThrottle(ScopedRateThrottle):
    scope = "otp"


class PasswordResetRateThrottle(ScopedRateThrottle):
    scope = "password_reset"


class VerifyPasswordRateThrottle(ScopedRateThrottle):
    scope = "verify_password"


class GoogleAuthRateThrottle(ScopedRateThrottle):
    scope = "google_auth"


class PortalLoginRateThrottle(SimpleRateThrottle):
    """Client Portal login (dashboard ClientViewSet.portal_login), limited
    per caller IP. Unlike ScopedRateThrottle it doesn't read a
    `throttle_scope` attribute off the view (a ViewSet @action can't set
    one), so the scope is fixed here and tuned in settings.py under
    DEFAULT_THROTTLE_RATES["portal_login"]."""

    scope = "portal_login"

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}


class EmailKeyedThrottle(SimpleRateThrottle):
    """Rate-limits by the `email` field in the POST body rather than by
    caller IP. Attach alongside an IP-based throttle on any endpoint that
    takes an email (login, send-otp, verify-otp, reset-password) so a
    distributed/rotating-IP attack against one account still gets capped.
    Requests with no email in the body are never limited by this class —
    they still hit the IP-based throttle.
    """

    scope = "email_lockout"

    def get_cache_key(self, request, view):
        email = (request.data.get("email") or "").strip().lower()
        if not email:
            return None
        return self.cache_format % {"scope": self.scope, "ident": email}


class PortalEmailKeyedThrottle(EmailKeyedThrottle):
    """Same idea as EmailKeyedThrottle, but with its own counter so failed
    Client Portal attempts for an email don't eat into the staff-login
    budget for that same email (and vice versa)."""

    scope = "portal_email_lockout"
