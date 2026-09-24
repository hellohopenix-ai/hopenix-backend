# Adds Client.date_of_birth so a client's birthday is stored on the server
# (and therefore reaches the Client Portal) instead of only in the admin's
# browser.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('dashboard', '0009_add_document_model'),
    ]

    operations = [
        migrations.AddField(
            model_name='client',
            name='date_of_birth',
            field=models.DateField(blank=True, null=True),
        ),
    ]