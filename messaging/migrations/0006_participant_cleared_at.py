from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("messaging", "0005_birthdaynotice"),
    ]

    operations = [
        migrations.AddField(
            model_name="participant",
            name="cleared_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
