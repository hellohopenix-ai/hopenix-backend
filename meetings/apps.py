from django.apps import AppConfig


class MeetingsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "meetings"

    def ready(self):
        from .reminders import start_reminder_thread

        start_reminder_thread()