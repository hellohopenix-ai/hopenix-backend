from django.apps import AppConfig


class ReportsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "reports"

    def ready(self):
        # Connects the model signals that record every create/update/delete
        # into ActivityLog — see reports/signals.py.
        from . import signals

        signals.connect_all()
