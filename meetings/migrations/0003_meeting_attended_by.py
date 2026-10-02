from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("meetings", "0002_meeting_reminder_flags"),
    ]

    operations = [
        migrations.AddField(
            model_name="meeting",
            name="attended_by",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
