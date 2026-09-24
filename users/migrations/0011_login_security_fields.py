from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0010_user_avatar'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='failed_login_attempts',
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='user',
            name='locked_until',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='otpcode',
            name='attempts',
            field=models.PositiveIntegerField(default=0),
        ),
    ]
