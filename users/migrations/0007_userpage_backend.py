# Generated for the UserPage.jsx backend (salary, ID card uploads, and
# role/module/individual access-control tables).

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

import users.models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('users', '0006_profile_per_user_upload_folders'),
    ]

    operations = [
        migrations.AddField(
            model_name='profile',
            name='salary',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True),
        ),
        migrations.AddField(
            model_name='profile',
            name='id_card_front',
            field=models.FileField(blank=True, null=True, upload_to=users.models.user_id_card_upload_path),
        ),
        migrations.AddField(
            model_name='profile',
            name='id_card_back',
            field=models.FileField(blank=True, null=True, upload_to=users.models.user_id_card_upload_path),
        ),
        migrations.CreateModel(
            name='RolePermission',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('role', models.CharField(max_length=20, unique=True)),
                ('pages', models.JSONField(blank=True, default=list)),
            ],
        ),
        migrations.CreateModel(
            name='ModulePermission',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('role', models.CharField(max_length=20)),
                ('module', models.CharField(max_length=100)),
                ('view', models.BooleanField(default=False)),
                ('create', models.BooleanField(default=False)),
                ('edit', models.BooleanField(default=False)),
                ('delete', models.BooleanField(default=False)),
            ],
            options={
                'unique_together': {('role', 'module')},
            },
        ),
        migrations.CreateModel(
            name='UserAccessOverride',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('mode', models.CharField(choices=[('custom', 'Custom'), ('full', 'Full Access'), ('none', 'No Access')], max_length=10)),
                ('pages', models.JSONField(blank=True, default=list)),
                ('user', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='access_override', to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.CreateModel(
            name='UserSubPageAccess',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('page', models.CharField(max_length=100)),
                ('mode', models.CharField(default='default', max_length=10)),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='sub_page_access', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'unique_together': {('user', 'page')},
            },
        ),
        migrations.CreateModel(
            name='AppSetting',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('key', models.CharField(max_length=100, unique=True)),
                ('value', models.BooleanField(default=False)),
            ],
        ),
    ]
