from datetime import datetime, time, timezone as dt_timezone

from django.apps import apps
from django.core.management.base import BaseCommand
from django.utils import timezone

from reports.models import ActivityLog
from reports.services import user_label
from reports.signals import TRACKED, _get, _object_repr, _sentence, _summary

User = None  # resolved lazily


def _aware(value):
    """DateField -> aware datetime at start of day (UTC); datetime -> aware."""
    if value is None:
        return timezone.now()
    if isinstance(value, datetime):
        return value if timezone.is_aware(value) else timezone.make_aware(value)
    return datetime.combine(value, time.min, tzinfo=dt_timezone.utc)


def _task_creator(task):
    """Task.created_by is a display NAME (string), not an FK."""
    name = (task.created_by or "").strip()
    if not name:
        return None, ""
    user = User.objects.filter(name__iexact=name).first()
    return user, name


# (model label, action, actor(obj) -> (user, fallback_name), when(obj))
SOURCES = [
    ("users.User", "register", lambda o: (o, ""), lambda o: o.date_joined),
    ("users.Profile", "create", lambda o: (o.user, ""), lambda o: o.created_at),
    ("projects.Project", "create", lambda o: (o.created_by, ""), lambda o: o.created_at),
    ("projects.ModuleFile", "upload", lambda o: (o.uploaded_by, ""), lambda o: o.uploaded_at),
    ("tasks.Task", "create", _task_creator, lambda o: o.created_on),
    ("tasks.TaskZipFile", "upload", lambda o: (o.uploaded_by, ""), lambda o: o.uploaded_at),
    ("dashboard.Client", "create", lambda o: (o.created_by, ""), lambda o: o.created_at),
    ("dashboard.Income", "create", lambda o: (o.created_by, ""), lambda o: o.created_at),
    ("sales.Sale", "create", lambda o: (o.created_by, ""), lambda o: o.created_at),
    ("expenses.Expense", "create", lambda o: (o.created_by, ""), lambda o: o.created_at),
    ("meetings.Meeting", "create", lambda o: (o.created_by_user, o.created_by), lambda o: o.created_at),
    ("employees.LeaveRequest", "create", lambda o: (o.employee, ""), lambda o: o.requested_at),
    ("reports.DailyReport", "create", lambda o: (o.user, o.user_name), lambda o: o.created_at),
]


class Command(BaseCommand):
    help = (
        "Create 'created / registered / uploaded' history rows for records that already existed "
        "before activity logging was installed, so past work shows up on the Reports page too. "
        "Safe to run more than once — anything already recorded is skipped. Messages are not backfilled."
    )

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Show what would be added without writing.")

    def handle(self, *args, dry_run, **options):
        global User
        from django.contrib.auth import get_user_model

        User = get_user_model()
        total = 0

        for label, action, actor_fn, when_fn in SOURCES:
            cfg = TRACKED.get(label)
            try:
                model = apps.get_model(label)
            except LookupError:
                continue
            if cfg is None:
                continue

            object_type = model._meta.model_name
            done = set(
                ActivityLog.objects.filter(object_type=object_type, action__in=("create", "register", "upload"))
                .values_list("object_id", flat=True)
            )

            batch = []
            for obj in model.objects.all().iterator(chunk_size=1000):
                if str(obj.pk) in done:
                    continue
                try:
                    user, fallback = actor_fn(obj)
                    repr_ = _object_repr(cfg, obj)
                    name = user_label(user) or fallback
                    description = f"Registered account “{repr_}”" if action == "register" else _sentence(cfg, action, repr_)
                    batch.append(ActivityLog(
                        user=user if getattr(user, "pk", None) else None,
                        actor_name=name[:255],
                        actor_email=(getattr(user, "email", "") or "")[:254],
                        actor_role=(getattr(user, "role", "") or "")[:20],
                        action=action,
                        module=cfg.module,
                        object_type=object_type,
                        object_id=str(obj.pk),
                        object_repr=repr_,
                        description=description[:500],
                        project=str(_get(cfg.project, obj, ""))[:255],
                        metadata={**_summary(obj, cfg), "backfilled": True},
                        created_at=_aware(when_fn(obj)),
                    ))
                except Exception as exc:  # one odd row must not stop the rest
                    self.stderr.write(f"  skipped {label} #{obj.pk}: {exc}")

            if batch and not dry_run:
                ActivityLog.objects.bulk_create(batch, batch_size=500)
            total += len(batch)
            self.stdout.write(f"{label:28s} {len(batch):>6d} {'would be ' if dry_run else ''}added")

        self.stdout.write(self.style.SUCCESS(f"{'Would add' if dry_run else 'Added'} {total} history rows."))
