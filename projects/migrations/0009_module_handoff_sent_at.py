from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("projects", "0008_module_url_length_assignee_team"),
    ]

    operations = [
        migrations.AddField(
            model_name="module",
            name="handoff_sent_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
