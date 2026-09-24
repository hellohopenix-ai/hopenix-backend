import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

import employees.models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("users", "0010_user_avatar"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="EmployeeExtra",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("employment_status", models.CharField(choices=[("Active", "Active"), ("On Leave", "On Leave")], default="Active", max_length=20)),
                ("rating", models.PositiveSmallIntegerField(default=5)),
                ("location", models.CharField(blank=True, default="", max_length=255)),
                ("manual_projects_assigned", models.PositiveIntegerField(default=0)),
                ("manual_projects_completed", models.PositiveIntegerField(default=0)),
                ("manual_tasks", models.PositiveIntegerField(default=0)),
                ("manual_tasks_completed", models.PositiveIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("user", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="employee_extra", to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.CreateModel(
            name="LeaveRequest",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("type", models.CharField(choices=[("Half Day", "Half Day"), ("Full Day", "Full Day")], max_length=20)),
                ("start_date", models.DateField()),
                ("end_date", models.DateField()),
                ("days", models.DecimalField(decimal_places=1, max_digits=5)),
                ("reason", models.TextField(blank=True, default="")),
                ("status", models.CharField(choices=[("pending", "Pending"), ("approved", "Approved"), ("rejected", "Rejected")], default="pending", max_length=10)),
                ("requested_at", models.DateTimeField(auto_now_add=True)),
                ("decided_at", models.DateTimeField(blank=True, null=True)),
                ("decided_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to=settings.AUTH_USER_MODEL)),
                ("employee", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="leave_requests", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-requested_at"]},
        ),
        migrations.CreateModel(
            name="Holiday",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("date", models.DateField(unique=True)),
                ("reason", models.CharField(blank=True, default="Company Holiday", max_length=255)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["date"]},
        ),
        migrations.CreateModel(
            name="Announcement",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("type", models.CharField(choices=[("promotion", "Promotion"), ("bonus", "Bonus"), ("post", "Post")], max_length=20)),
                ("detail", models.CharField(blank=True, default="", max_length=255)),
                ("message", models.TextField(blank=True, default="")),
                ("image", models.FileField(blank=True, null=True, upload_to=employees.models.announcement_image_path)),
                ("pdf_file", models.FileField(blank=True, null=True, upload_to=employees.models.announcement_pdf_path)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("seen_by", models.JSONField(blank=True, default=list)),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to=settings.AUTH_USER_MODEL)),
                ("employee", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="announcements", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-created_at"]},
        ),
    ]
