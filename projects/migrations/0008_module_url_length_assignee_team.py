from django.db import migrations, models


def add_module_assignees_to_team(apps, schema_editor):
    """Backfill: everybody who already has a module assigned joins that
    project's team, so existing projects show up for them too."""
    Module = apps.get_model("projects", "Module")
    for module in Module.objects.exclude(assignee__isnull=True).select_related("project"):
        module.project.team.add(module.assignee_id)


class Migration(migrations.Migration):

    dependencies = [
        ("projects", "0007_module_url_approval"),
    ]

    operations = [
        migrations.AlterField(
            model_name="module",
            name="url",
            field=models.URLField(blank=True, default="", max_length=500),
        ),
        migrations.RunPython(add_module_assignees_to_team, migrations.RunPython.noop),
    ]
