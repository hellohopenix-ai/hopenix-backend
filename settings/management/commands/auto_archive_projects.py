from django.core.management.base import BaseCommand

from settings.rules import archive_old_completed_projects


class Command(BaseCommand):
    help = "Archive projects that have been Completed for 30+ days (Settings -> Projects -> Auto-archive)."

    def handle(self, *args, **options):
        n = archive_old_completed_projects()
        self.stdout.write(self.style.SUCCESS(f"Archived {n} project(s)."))
