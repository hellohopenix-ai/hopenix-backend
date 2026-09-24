import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('dashboard', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='Task',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('title', models.CharField(max_length=255)),
                ('description', models.TextField(blank=True)),
                ('requirements', models.TextField(blank=True)),
                ('project', models.CharField(max_length=255)),
                ('assignees', models.JSONField(blank=True, default=list)),
                ('assignee_role', models.CharField(blank=True, max_length=150)),
                ('priority', models.CharField(choices=[('High', 'High'), ('Medium', 'Medium'), ('Low', 'Low')], default='Medium', max_length=10)),
                ('status', models.CharField(choices=[('Pending', 'Pending'), ('In Progress', 'In Progress'), ('In Review', 'In Review'), ('Completed', 'Completed'), ('Overdue', 'Overdue')], default='Pending', max_length=20)),
                ('due_date', models.DateField(blank=True, null=True)),
                ('created_by', models.CharField(blank=True, max_length=150)),
                ('created_on', models.DateField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('progress', models.PositiveSmallIntegerField(default=0)),
                ('locked', models.BooleanField(default=False)),
                ('attachment', models.CharField(blank=True, max_length=1000)),
                ('attachments', models.JSONField(blank=True, default=list)),
                ('sample_files', models.JSONField(blank=True, default=list)),
                ('subtasks', models.JSONField(blank=True, default=list)),
                ('module_name', models.CharField(blank=True, max_length=255)),
                ('module_project_name', models.CharField(blank=True, max_length=255)),
                ('module_task_key', models.CharField(blank=True, max_length=255)),
                ('role_template', models.CharField(blank=True, max_length=100)),
                ('requires_link', models.BooleanField(default=False)),
                ('from_client_module', models.BooleanField(default=False)),
                ('client', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='tasks', to='dashboard.client')),
            ],
            options={
                'ordering': ['-id'],
            },
        ),
    ]
