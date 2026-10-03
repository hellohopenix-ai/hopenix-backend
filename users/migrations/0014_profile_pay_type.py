from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0013_seed_role_catalog"),
    ]

    operations = [
        migrations.AddField(
            model_name="profile",
            name="pay_type",
            field=models.CharField(
                choices=[("salary", "Monthly salary"), ("per_project", "Per project")],
                default="salary",
                max_length=20,
            ),
        ),
    ]
