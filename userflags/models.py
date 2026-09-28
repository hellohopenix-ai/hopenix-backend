from django.conf import settings
from django.db import models


class UserFlag(models.Model):
    """One small per-user UI flag (sidebar "new task" dot, birthday message
    already delivered, "seen" markers, ...).

    These used to live only in ONE browser's localStorage, so the same person
    on a phone + a laptop saw different dots and got the same notification
    twice. Keeping them here (keyed by user) makes them follow the account.
    The value is any small JSON document; the frontend decides its shape."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="ui_flags")
    key = models.CharField(max_length=80)
    value = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "key"], name="uniq_userflag_user_key")]
        ordering = ["key"]

    def __str__(self):
        return f"{self.user_id}:{self.key}"
