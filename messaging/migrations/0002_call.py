# Adds the Call model (voice/video calling between two users, signalled
# over the existing per-user websocket — see messaging/consumers.py and
# messaging/views.py). Nothing about Conversation/Participant/Message
# changes; this only adds a new table.

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('messaging', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='Call',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('call_type', models.CharField(choices=[('audio', 'Audio'), ('video', 'Video')], default='audio', max_length=10)),
                ('status', models.CharField(choices=[('ringing', 'Ringing'), ('ongoing', 'Ongoing'), ('ended', 'Ended'), ('missed', 'Missed'), ('rejected', 'Rejected')], default='ringing', max_length=10)),
                ('started_at', models.DateTimeField(auto_now_add=True)),
                ('answered_at', models.DateTimeField(blank=True, null=True)),
                ('ended_at', models.DateTimeField(blank=True, null=True)),
                ('duration_seconds', models.PositiveIntegerField(default=0)),
                ('callee', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='calls_received', to=settings.AUTH_USER_MODEL)),
                ('caller', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='calls_made', to=settings.AUTH_USER_MODEL)),
                ('conversation', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='calls', to='messaging.conversation')),
            ],
            options={
                'ordering': ['-started_at'],
            },
        ),
    ]
