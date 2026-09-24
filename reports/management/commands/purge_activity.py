from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from reports.models import ActivityLog


class Command(BaseCommand):
    help = "Delete activity log rows older than N days (retention). Nothing is deleted unless you run this."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, required=True, help="Keep this many days; older rows are deleted.")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, days, dry_run, **options):
        if days < 30:
            raise CommandError("Refusing to keep less than 30 days of audit history.")
        cutoff = timezone.now() - timedelta(days=days)
        qs = ActivityLog.objects.filter(created_at__lt=cutoff)
        n = qs.count()
        if dry_run:
            self.stdout.write(f"{n} rows older than {days} days would be deleted.")
            return
        qs._raw_delete(qs.db)  # bulk delete; skips per-row signals (there are none on ActivityLog)
        self.stdout.write(self.style.SUCCESS(f"Deleted {n} rows older than {days} days."))
