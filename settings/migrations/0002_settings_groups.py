from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('settings', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='ProjectSettings',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('default_view', models.CharField(blank=True, default='Kanban Board', max_length=30)),
                ('auto_archive', models.BooleanField(default=True)),
                ('require_code', models.BooleanField(default=False)),
                ('allow_guest_access', models.BooleanField(default=False)),
                ('time_tracking', models.BooleanField(default=True)),
                ('categories', models.JSONField(blank=True, default=list)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
        ),
        migrations.CreateModel(
            name='TaskSettings',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('default_view', models.CharField(blank=True, default='Board View', max_length=30)),
                ('auto_assign_lead', models.BooleanField(default=False)),
                ('allow_subtasks', models.BooleanField(default=True)),
                ('require_due_date', models.BooleanField(default=False)),
                ('send_reminders', models.BooleanField(default=True)),
                ('statuses', models.JSONField(blank=True, default=list)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
        ),
        migrations.CreateModel(
            name='IncomeSettings',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('default_account', models.CharField(blank=True, default='', max_length=100)),
                ('recurring_income', models.BooleanField(default=True)),
                ('auto_invoice', models.BooleanField(default=False)),
                ('categories', models.JSONField(blank=True, default=list)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
        ),
        migrations.CreateModel(
            name='ExpenseSettings',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('approval_threshold', models.DecimalField(decimal_places=2, default=0, max_digits=12)),
                ('require_receipt', models.BooleanField(default=True)),
                ('auto_categorize', models.BooleanField(default=False)),
                ('categories', models.JSONField(blank=True, default=list)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
        ),
        migrations.CreateModel(
            name='SalesSettings',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('tax_rate', models.DecimalField(decimal_places=2, default=0, max_digits=5)),
                ('invoice_prefix', models.CharField(blank=True, default='INV-', max_length=20)),
                ('payment_terms', models.CharField(blank=True, default='Net 15', max_length=50)),
                ('discount', models.DecimalField(decimal_places=2, default=0, max_digits=5)),
                ('auto_invoice_number', models.BooleanField(default=True)),
                ('payment_reminders', models.BooleanField(default=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
        ),
    ]
