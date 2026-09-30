from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("meetings", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="meeting",
            name="day_reminder_sent",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="meeting",
            name="soon_reminder_sent",
            field=models.BooleanField(default=False),
        ),
    ]
