from django.db import migrations

# Mirrors frontend/src/pages/UserPage.jsx's INITIAL_ROLES exactly, so
# existing installs see the same starting list they always have instead
# of an empty Role Management card after this upgrade.
DEFAULT_ROLES = [
    {"name": "Super Admin", "tag": "System", "access": "Full Access", "locked": True},
    {"name": "Admin", "tag": "", "access": "Full Access", "locked": False},
    {"name": "Manager", "tag": "", "access": "Custom Access", "locked": False},
    {"name": "Employee", "tag": "", "access": "Limited Access", "locked": False},
    {"name": "Client", "tag": "", "access": "Limited Access", "locked": False},
    {"name": "Accountant", "tag": "", "access": "Custom Access", "locked": False},
]


def seed_roles(apps, schema_editor):
    RoleCatalogEntry = apps.get_model("users", "RoleCatalogEntry")
    for row in DEFAULT_ROLES:
        RoleCatalogEntry.objects.get_or_create(name=row["name"], defaults=row)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0012_rolecatalogentry"),
    ]

    operations = [
        migrations.RunPython(seed_roles, noop),
    ]
