from django.db import migrations, models
from django.utils import timezone


def mark_existing_as_handled(apps, schema_editor):
    """Files/links that already exist were never part of the new approval
    flow - mark them as already announced + forwarded so nobody gets old
    files re-sent the first time an admin presses Approve."""
    ModuleFile = apps.get_model("projects", "ModuleFile")
    Module = apps.get_model("projects", "Module")
    now = timezone.now()
    ModuleFile.objects.filter(admin_notified_at__isnull=True).update(admin_notified_at=now)
    ModuleFile.objects.filter(forwarded_at__isnull=True).update(forwarded_at=now)
    for m in Module.objects.exclude(url=""):
        Module.objects.filter(pk=m.pk).update(handoff_url_notified=m.url, handoff_url_forwarded=m.url)


class Migration(migrations.Migration):

    dependencies = [
        ("projects", "0010_module_handoff_requested_at"),
    ]

    operations = [
        migrations.AddField(
            model_name="module",
            name="handoff_url_notified",
            field=models.CharField(blank=True, default="", max_length=500),
        ),
        migrations.AddField(
            model_name="module",
            name="handoff_url_forwarded",
            field=models.CharField(blank=True, default="", max_length=500),
        ),
        migrations.AddField(
            model_name="modulefile",
            name="admin_notified_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="modulefile",
            name="forwarded_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(mark_existing_as_handled, migrations.RunPython.noop),
    ]
