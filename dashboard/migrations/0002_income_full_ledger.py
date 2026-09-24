import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('dashboard', '0001_initial'),
    ]

    operations = [
        migrations.RenameField(
            model_name='income',
            old_name='title',
            new_name='description',
        ),
        migrations.RenameField(
            model_name='income',
            old_name='source',
            new_name='project',
        ),
        migrations.AlterField(
            model_name='income',
            name='description',
            field=models.CharField(max_length=255),
        ),
        migrations.AlterField(
            model_name='income',
            name='project',
            field=models.CharField(blank=True, default='', max_length=150),
        ),
        migrations.AddField(
            model_name='income',
            name='client',
            field=models.CharField(blank=True, default='', max_length=150),
        ),
        migrations.AddField(
            model_name='income',
            name='method',
            field=models.CharField(
                choices=[
                    ('Bank Transfer', 'Bank Transfer'),
                    ('JazzCash', 'JazzCash'),
                    ('Easypaisa', 'Easypaisa'),
                    ('Cash in Hand', 'Cash in Hand'),
                ],
                default='Bank Transfer',
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name='income',
            name='status',
            field=models.CharField(
                choices=[('Received', 'Received'), ('Pending', 'Pending'), ('Overdue', 'Overdue')],
                default='Received',
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name='income',
            name='received_on',
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='income',
            name='updated_at',
            field=models.DateTimeField(auto_now=True),
        ),
        migrations.AddField(
            model_name='income',
            name='created_by',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='income_entries',
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterModelOptions(
            name='income',
            options={'ordering': ['-date', '-created_at']},
        ),
        migrations.AddIndex(
            model_name='income',
            index=models.Index(fields=['status'], name='dashboard_i_status_5e6c8f_idx'),
        ),
        migrations.AddIndex(
            model_name='income',
            index=models.Index(fields=['project'], name='dashboard_i_project_1a2b3c_idx'),
        ),
    ]
