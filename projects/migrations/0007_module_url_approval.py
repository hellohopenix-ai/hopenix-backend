# Review/approval gate for a module's live/staging link (Module.url) — the
# link counterpart of 0006_modulefile_approval. Task Page link -> Client Page
# review -> admin approves -> Client Portal visibility.

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def mark_existing_links_approved(apps, schema_editor):
    # Same precedent as 0006: links that were already live on the Client
    # Portal before this gate existed stay visible (nobody reviewed them, but
    # hiding them all overnight would be a regression). Only links pasted
    # AFTER this migration start out pending review.
    Module = apps.get_model('projects', 'Module')
    Module.objects.exclude(url='').update(url_approved=True)


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('projects', '0006_modulefile_approval'),
    ]

    operations = [
        migrations.AddField(
            model_name='module',
            name='url_approved',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='module',
            name='url_approved_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='module',
            name='url_approved_by',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='+',
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.RunPython(mark_existing_links_approved, migrations.RunPython.noop),
    ]
