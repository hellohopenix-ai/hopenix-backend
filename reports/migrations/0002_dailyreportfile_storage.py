import reports.models
import reports.storage
from django.db import migrations, models


class Migration(migrations.Migration):
    """Storage/upload-path change + file path column widened from 100 to 255
    characters (long phone file names overflowed it). No data is touched."""

    dependencies = [
        ('reports', '0001_initial'),
    ]

    operations = [
        migrations.AlterField(
            model_name='dailyreportfile',
            name='file',
            field=models.FileField(
                max_length=255,
                storage=reports.storage.daily_report_storage,
                upload_to=reports.models.daily_report_file_path,
            ),
        ),
    ]
