from django.apps import AppConfig


class SettingsConfig(AppConfig):
    """The 'Settings' page's own backend — General/Company info,
    Notification preferences, Security (2FA + change password), and
    Billing. Kept as its own app (separate from `users`) because it's
    organization/account-level configuration, not user-account
    management — same split SettingsPage.jsx already makes with its own
    tabs, just mirrored on the backend.

    NOTE: an app literally named "settings" is safe here because nothing
    in this project ever does a bare `import settings` — every file that
    needs Django's real settings does `from django.conf import settings`
    (see users/views.py, hopenix/settings.py itself), which always
    resolves to django.conf's LazySettings object, not this app. Still,
    if that ever bothers you, this is the one place to rename (this
    class, the `name` below, and the folder) to something like
    `app_settings` instead — nothing else references the folder name
    directly.
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "settings"
