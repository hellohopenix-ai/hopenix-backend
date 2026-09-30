from django.core.management.base import BaseCommand

from meetings.reminders import send_due_reminders


class Command(BaseCommand):
    help = "Send due meeting reminders once (only needed if you run it from cron instead of the built-in thread)."

    def handle(self, *args, **options):
        send_due_reminders()
        self.stdout.write("done")
