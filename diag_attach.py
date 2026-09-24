from dashboard.models import Client
from tasks.models import Task, TaskZipFile
from projects.models import Module, ModuleFile
from dashboard.serializers import ClientSerializer
from rest_framework.test import APIRequestFactory
from django.contrib.auth import get_user_model

User = get_user_model()

print("=== DB STATE ===")
print("ModuleFile rows:", ModuleFile.objects.count())
print("TaskZipFile rows:", TaskZipFile.objects.count())

print()
print("=== TASKS WITH module_id SET ===")
for t in Task.objects.filter(module__isnull=False).select_related("module"):
    print("  Task id=%d title=%s module_id=%s attach_len=%d" % (t.id, t.title[:30], t.module_id, len(t.attachments or [])))

print()
print("=== CLIENT-MODULE TASKS WITH ATTACHMENTS ===")
for t in Task.objects.filter(from_client_module=True).exclude(attachments__exact=[]).order_by("-id")[:6]:
    print("  Task id=%d module_id=%s title=%s" % (t.id, t.module_id, t.title[:30]))
    print("    attachments:", t.attachments)

print()
print("=== SERIALIZER OUTPUT ===")
factory = APIRequestFactory()
req = factory.get("/")
req.user = User.objects.filter(is_staff=True).first() or User.objects.first()

for client in Client.objects.all():
    data = ClientSerializer(client, context={"request": req}).data
    projects = data.get("projects", [])
    print("Client:", client.name, "id=%d" % client.id, "projects=%d" % len(projects))
    for p in projects:
        mods = p.get("modules", [])
        print("  Project:", p["name"])
        for m in mods:
            att = m.get("attachments", [])
            print("    Module:", m["name"], "id=%s" % m["id"], "attach=%d" % len(att))
            for a in att:
                print("      ->", a)
            for s in m.get("subModules", []):
                sa = s.get("attachments", [])
                if sa:
                    print("    Sub:", s["name"], "attach=%d" % len(sa))
                    for a in sa:
                        print("      ->", a)
