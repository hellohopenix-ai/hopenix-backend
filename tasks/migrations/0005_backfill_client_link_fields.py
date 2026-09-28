from django.db import migrations


def backfill(apps, schema_editor):
    """Fill the new columns for tasks that already exist.

    - moduleId / subModuleId are the last two "::"-separated parts of the
      task's module_task_key ("<clientId>::<project>::<moduleId>::<subModuleId>"),
      so they can be recovered exactly for every module-generated task.
    - fromClientAssignment mirrors the frontend's own detection of the
      "New client assigned: ..." tasks the system creates.
    """
    Task = apps.get_model("tasks", "Task")

    for task in Task.objects.exclude(module_task_key="").only("id", "module_task_key"):
        parts = task.module_task_key.split("::")
        if len(parts) < 4:
            continue
        Task.objects.filter(pk=task.pk).update(
            client_module_id=parts[-2],
            sub_module_id=parts[-1],
        )

    Task.objects.filter(created_by="System", title__istartswith="New client assigned: ").update(
        from_client_assignment=True
    )


class Migration(migrations.Migration):

    dependencies = [
        ("tasks", "0004_task_client_link_fields"),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
