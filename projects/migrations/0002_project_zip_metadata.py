# Generated manually to add Zip Files page metadata (original file name,
# size, upload timestamp) to Project.completed_zip. Run normally with:
#   python manage.py migrate projects

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='project',
            name='zip_original_name',
            field=models.CharField(blank=True, default='', max_length=255),
        ),
        migrations.AddField(
            model_name='project',
            name='zip_size',
            field=models.PositiveBigIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='project',
            name='zip_uploaded_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]