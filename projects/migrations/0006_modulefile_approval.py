# Generated for the review/approval gate (Task Page upload -> Client Page
# review -> approve -> Client Portal visibility). See ModuleFile.approved.

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def mark_existing_files_approved(apps, schema_editor):
    # Don't break existing working features: files uploaded before this
    # gate existed were already live on the Client Portal. Without this,
    # every one of them would default to approved=False and vanish from
    # the Portal the moment this migration runs, even though nobody
    # reviewed anything — that's a regression, not the intended change.
    # Only uploads made AFTER this point start out pending_review.
    ModuleFile = apps.get_model('projects', 'ModuleFile')
    ModuleFile.objects.update(approved=True)


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('projects', '0005_module_parent'),
    ]

    operations = [
        migrations.AddField(
            model_name='modulefile',
            name='approved',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='modulefile',
            name='approved_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='modulefile',
            name='approved_by',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='+',
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.RunPython(mark_existing_files_approved, migrations.RunPython.noop),
    ]
