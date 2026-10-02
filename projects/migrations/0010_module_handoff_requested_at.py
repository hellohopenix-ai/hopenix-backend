from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("projects", "0009_module_handoff_sent_at"),
    ]

    operations = [
        migrations.AddField(
            model_name="module",
            name="handoff_requested_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
