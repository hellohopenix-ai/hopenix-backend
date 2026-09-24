import io
import shutil
import tempfile
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from users.models import UserSubPageAccess

from .models import ActivityLog, CustomReport, DailyReport, DailyReportFile, ReportOverride
from .services import log_activity
from .uploads import sniff

User = get_user_model()

MEDIA = tempfile.mkdtemp(prefix="hopenix_reports_test_")

# Smallest valid-looking headers — sniff() only reads the first bytes.
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64


def make_user(email, role="employee", name=None, status="approved", **extra):
    u = User.objects.create_user(email=email, password="pass12345", role=role,
                                 name=name or email.split("@")[0].title(), status=status, **extra)
    return u


def client_for(user):
    token, _ = Token.objects.get_or_create(user=user)
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
    return c


@override_settings(MEDIA_ROOT=MEDIA)
class ReportsTestBase(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA, ignore_errors=True)

    def setUp(self):
        self.admin = make_user("admin@x.com", "admin", "Ada Admin")
        self.ali = make_user("ali@x.com", "employee", "Ali Khan")
        self.sara = make_user("sara@x.com", "employee", "Sara Noor")
        self.a = client_for(self.admin)
        self.ali_c = client_for(self.ali)
        self.sara_c = client_for(self.sara)
        ActivityLog.objects.all().delete()  # ignore the "registered" rows made by setUp


class RecordingTests(ReportsTestBase):
    """The core promise: everything a user does over the API is recorded."""

    def test_task_create_update_delete_recorded_with_actor_and_diff(self):
        r = self.ali_c.post("/api/tasks/tasks/", {"title": "Fix login", "project": "Alpha", "assignees": ["Ali Khan"]}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        tid = r.json()["id"]

        log = ActivityLog.objects.get(action="create", object_type="task")
        self.assertEqual(log.user, self.ali)
        self.assertEqual(log.actor_name, "Ali Khan")
        self.assertEqual(log.module, "Tasks")
        self.assertEqual(log.project, "Alpha")
        self.assertIn("Fix login", log.description)
        self.assertEqual(log.method, "POST")

        r = self.ali_c.patch(f"/api/tasks/tasks/{tid}/", {"status": "Completed"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        upd = ActivityLog.objects.get(action="update", object_type="task")
        self.assertEqual(upd.changes["status"], {"from": "Pending", "to": "Completed"})
        self.assertEqual(upd.user, self.ali)
        self.assertIn("Pending → Completed", upd.description)

        # Server-side permission enforcement (users/access.py) now matches
        # the frontend's own defaults exactly: role "employee" has no
        # delete flag on the Tasks module (see AuthContext.jsx's
        # DEFAULT_MODULE_PERMISSIONS.employee.Tasks), so Ali himself can no
        # longer delete this task — only someone with the flag (admin here)
        # can. This is the intended lockdown, not a regression: the delete
        # action is still recorded with the actual actor.
        self.assertEqual(self.ali_c.delete(f"/api/tasks/tasks/{tid}/").status_code, 403)
        self.assertEqual(self.a.delete(f"/api/tasks/tasks/{tid}/").status_code, 204)
        self.assertTrue(ActivityLog.objects.filter(action="delete", object_type="task", user=self.admin).exists())

    def test_noop_save_writes_nothing(self):
        r = self.ali_c.post("/api/tasks/tasks/", {"title": "T", "project": "P"}, format="json")
        tid = r.json()["id"]
        before = ActivityLog.objects.count()
        self.ali_c.patch(f"/api/tasks/tasks/{tid}/", {"title": "T"}, format="json")
        self.assertEqual(ActivityLog.objects.count(), before)

    def test_two_users_are_attributed_separately(self):
        self.ali_c.post("/api/tasks/tasks/", {"title": "A", "project": "P"}, format="json")
        self.sara_c.post("/api/tasks/tasks/", {"title": "B", "project": "P"}, format="json")
        self.assertEqual(ActivityLog.objects.get(description__contains="“A”").user, self.ali)
        self.assertEqual(ActivityLog.objects.get(description__contains="“B”").user, self.sara)

    def test_login_logout_and_failed_login(self):
        c = APIClient()
        bad = c.post("/api/auth/login/", {"email": "ali@x.com", "password": "wrong"}, format="json")
        self.assertEqual(bad.status_code, 401)
        f = ActivityLog.objects.get(action="login_failed")
        self.assertEqual(f.actor_email, "ali@x.com")
        # repeated failures within 30s are collapsed so the endpoint can't be used to flood the table
        c.post("/api/auth/login/", {"email": "ali@x.com", "password": "wrong"}, format="json")
        self.assertEqual(ActivityLog.objects.filter(action="login_failed").count(), 1)

        ok = c.post("/api/auth/login/", {"email": "ali@x.com", "password": "pass12345"}, format="json")
        self.assertEqual(ok.status_code, 200, ok.content)
        self.assertEqual(ActivityLog.objects.get(action="login").user, self.ali)

        c.credentials(HTTP_AUTHORIZATION=f"Token {ok.json()['token']}")
        c.post("/api/auth/logout/")
        self.assertEqual(ActivityLog.objects.get(action="logout").user, self.ali)

    def test_password_values_never_stored(self):
        self.ali.set_password("brand-new-secret-99")
        with_state = ActivityLog.objects.count()
        # make the change inside a request so it has an actor
        from .context import RequestState, reset_state, set_state
        from django.test import RequestFactory
        req = RequestFactory().post("/x/")
        req.user = self.ali
        tok = set_state(RequestState(req))
        try:
            self.ali.save()
        finally:
            reset_state(tok)
        log = ActivityLog.objects.filter(object_type="user", action="update").latest("id")
        self.assertEqual(log.changes["password"], {"from": "•••", "to": "•••"})
        self.assertNotIn("brand-new-secret-99", str(log.changes))
        self.assertGreater(ActivityLog.objects.count(), with_state)

    def test_presence_ping_is_not_logged(self):
        User.objects.filter(pk=self.ali.pk).update(last_active_at=timezone.now())
        self.ali_c.get("/api/auth/me/")
        self.assertEqual(ActivityLog.objects.count(), 0)

    def test_status_approval_is_recorded_as_approve(self):
        pending = make_user("new@x.com", "employee", "Newbie", status="pending")
        ActivityLog.objects.all().delete()
        from .context import RequestState, reset_state, set_state
        from django.test import RequestFactory
        req = RequestFactory().post("/x/")
        req.user = self.admin
        tok = set_state(RequestState(req))
        try:
            pending.status = "approved"
            pending.save()
        finally:
            reset_state(tok)
        log = ActivityLog.objects.get()
        self.assertEqual(log.action, "approve")
        self.assertEqual(log.user, self.admin)
        self.assertEqual(log.object_repr, "Newbie")

    def test_actor_snapshot_survives_user_deletion(self):
        self.ali_c.post("/api/tasks/tasks/", {"title": "Keep me", "project": "P"}, format="json")
        self.ali.delete()
        log = ActivityLog.objects.get(description__contains="Keep me", action="create")
        self.assertIsNone(log.user)
        self.assertEqual(log.actor_name, "Ali Khan")

    def test_log_is_append_only(self):
        entry = log_activity(action="view", user=self.ali, module="Tasks", description="Opened Tasks")
        entry.description = "tampered"
        with self.assertRaises(PermissionError):
            entry.save()

    def test_logging_failure_never_breaks_the_action(self):
        from unittest import mock
        with mock.patch("reports.models.ActivityLog.objects.create", side_effect=RuntimeError("db down")):
            r = self.ali_c.post("/api/tasks/tasks/", {"title": "Still works", "project": "P"}, format="json")
        self.assertEqual(r.status_code, 201)

    def test_project_team_change_recorded(self):
        from projects.models import Project
        p = Project.objects.create(name="Alpha", created_by=self.admin)
        ActivityLog.objects.all().delete()
        from .context import RequestState, reset_state, set_state
        from django.test import RequestFactory
        req = RequestFactory().post("/x/")
        req.user = self.admin
        tok = set_state(RequestState(req))
        try:
            p.team.add(self.ali)
        finally:
            reset_state(tok)
        log = ActivityLog.objects.get()
        self.assertIn("Ali Khan", log.description)
        self.assertEqual(log.project, "Alpha")


class ActivityApiTests(ReportsTestBase):
    def setUp(self):
        super().setUp()
        self.ali_c.post("/api/tasks/tasks/", {"title": "Ali task", "project": "Alpha"}, format="json")
        self.sara_c.post("/api/tasks/tasks/", {"title": "Sara task", "project": "Beta"}, format="json")

    def test_admin_sees_everyone(self):
        r = self.a.get("/api/reports/activity/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["scope"], "all")
        self.assertEqual(r.json()["count"], 2)

    def test_employee_sees_only_own_even_if_asking_for_others(self):
        r = self.ali_c.get(f"/api/reports/activity/?user={self.sara.id}")
        self.assertEqual(r.json()["scope"], "own")
        self.assertEqual(r.json()["count"], 0)
        r = self.ali_c.get("/api/reports/activity/")
        self.assertEqual({row["userId"] for row in r.json()["results"]}, {self.ali.id})

    def test_full_reports_access_grants_everyone_view(self):
        UserSubPageAccess.objects.create(user=self.sara, page="Reports", mode="full")
        data = self.sara_c.get("/api/reports/activity/").json()
        self.assertEqual(data["scope"], "all")
        self.assertEqual({row["userId"] for row in data["results"] if row["module"] == "Tasks"}, {self.ali.id, self.sara.id})

    def test_filters(self):
        base = "/api/reports/activity/"
        self.assertEqual(self.a.get(f"{base}?user={self.ali.id}").json()["count"], 1)
        self.assertEqual(self.a.get(f"{base}?project=Beta").json()["count"], 1)
        self.assertEqual(self.a.get(f"{base}?module=Tasks&action=create").json()["count"], 2)
        self.assertEqual(self.a.get(f"{base}?module=Sales").json()["count"], 0)
        self.assertEqual(self.a.get(f"{base}?q=sara").json()["count"], 1)
        self.assertEqual(self.a.get(f"{base}?category=Task").json()["count"], 2)

    def test_date_range_filter_and_validation(self):
        old = ActivityLog.objects.first()
        ActivityLog.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=90))
        self.assertEqual(self.a.get("/api/reports/activity/?range=last7").json()["count"], 1)
        self.assertEqual(self.a.get("/api/reports/activity/?range=all").json()["count"], 2)
        self.assertEqual(self.a.get("/api/reports/activity/?range=nonsense").status_code, 400)
        self.assertEqual(self.a.get("/api/reports/activity/?start=2026-02-01&end=2026-01-01").status_code, 400)
        self.assertEqual(self.a.get("/api/reports/activity/?start=garbage").status_code, 400)

    def test_requires_login(self):
        self.assertEqual(APIClient().get("/api/reports/activity/").status_code, 401)

    def test_filters_endpoint_scopes_users(self):
        self.assertGreaterEqual(len(self.a.get("/api/reports/activity/filters/").json()["users"]), 3)
        own = self.ali_c.get("/api/reports/activity/filters/").json()
        self.assertEqual([u["id"] for u in own["users"]], [self.ali.id])

    def test_track_endpoint_and_dedupe(self):
        body = {"module": "Tasks", "action": "view", "description": "Opened Tasks page", "page": "/tasks"}
        self.assertEqual(self.ali_c.post("/api/reports/activity/track/", body, format="json").status_code, 201)
        r = self.ali_c.post("/api/reports/activity/track/", body, format="json")
        self.assertEqual(r.json(), {"logged": False})
        self.assertEqual(ActivityLog.objects.filter(action="view", user=self.ali).count(), 1)
        bad = self.ali_c.post("/api/reports/activity/track/", {**body, "action": "delete"}, format="json")
        self.assertEqual(bad.status_code, 400)  # a browser can't forge a "delete"/"login" entry

    def test_csv_export_neutralises_formulas(self):
        log_activity(action="view", user=self.ali, module="Tasks", description='=HYPERLINK("http://evil","x")')
        r = self.a.get("/api/reports/activity/export/")
        self.assertEqual(r.status_code, 200)
        body = b"".join(r.streaming_content).decode("utf-8-sig")
        self.assertIn("'=HYPERLINK", body)
        self.assertNotIn(',=HYPERLINK', body)
        self.assertTrue(ActivityLog.objects.filter(action="export").exists())


class UserReportTests(ReportsTestBase):
    def setUp(self):
        super().setUp()
        for _ in range(3):
            log_activity(action="create", user=self.ali, module="Tasks", description="x")
        log_activity(action="update", user=self.ali, module="Sales", description="y")
        log_activity(action="create", user=self.sara, module="Tasks", description="z")

    def test_per_user_rows_include_inactive_users_and_sort_by_activity(self):
        r = self.a.get("/api/reports/users/?range=all")
        self.assertEqual(r.status_code, 200)
        rows = r.json()["results"]
        self.assertEqual(rows[0]["userId"], self.ali.id)
        self.assertEqual(rows[0]["totalActions"], 4)
        self.assertEqual(rows[0]["byModule"], {"Tasks": 3, "Sales": 1})
        self.assertEqual(rows[0]["byAction"], {"create": 3, "update": 1})
        admin_row = next(x for x in rows if x["userId"] == self.admin.id)
        self.assertEqual(admin_row["totalActions"], 0)

    def test_users_list_is_admin_only_but_me_is_open(self):
        self.assertEqual(self.ali_c.get("/api/reports/users/").status_code, 403)
        self.assertEqual(self.ali_c.get(f"/api/reports/users/{self.sara.id}/").status_code, 403)
        me = self.ali_c.get("/api/reports/users/me/?range=all")
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["userId"], self.ali.id)
        self.assertEqual(me.json()["totalActions"], 4)

    def test_user_detail(self):
        r = self.a.get(f"/api/reports/users/{self.ali.id}/?range=last7")
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertEqual(len(d["timeline"]), 7)
        self.assertEqual(sum(day["count"] for day in d["timeline"]), 4)
        self.assertEqual(len(d["recentActivity"]), 4)
        self.assertIn("tasksAssigned", d["workload"])
        self.assertEqual(self.a.get("/api/reports/users/999999/").status_code, 404)
        self.assertEqual(self.a.get("/api/reports/users/abc/").status_code, 404)

    def test_backfill_is_idempotent(self):
        from django.core.management import call_command
        from projects.models import Project
        Project.objects.create(name="Old project", created_by=self.admin)
        ActivityLog.objects.all().delete()
        call_command("backfill_activity", stdout=io.StringIO())
        n = ActivityLog.objects.count()
        self.assertTrue(ActivityLog.objects.filter(object_type="project", metadata__backfilled=True).exists())
        self.assertTrue(ActivityLog.objects.filter(action="register", user=self.ali).exists())
        call_command("backfill_activity", stdout=io.StringIO())
        self.assertEqual(ActivityLog.objects.count(), n)


class SummaryAndCatalogTests(ReportsTestBase):
    def setUp(self):
        super().setUp()
        from projects.models import Project
        from tasks.models import Task
        self.p = Project.objects.create(name="Alpha", created_by=self.admin, budget=1000, spent=250, status="In Progress")
        self.p.team.add(self.ali)
        Task.objects.create(title="T1", project="Alpha", assignees=["Ali Khan"], status="Completed")
        Task.objects.create(title="T2", project="Alpha", assignees=["Ali Khan"], status="Pending",
                            due_date=timezone.localdate() - timedelta(days=3))
        log_activity(action="create", user=self.ali, module="Tasks", description="x")

    def test_summary(self):
        r = self.a.get("/api/reports/summary/?range=all")
        self.assertEqual(r.status_code, 200, r.content)
        s = r.json()
        self.assertEqual(s["stats"]["totalProjects"], 1)
        self.assertEqual(s["stats"]["activeProjects"], 1)
        self.assertEqual(s["stats"]["totalTasks"], 2)
        self.assertEqual(s["stats"]["completedTasks"], 1)
        self.assertEqual(s["stats"]["overdueTasks"], 1)
        self.assertEqual(s["stats"]["totalBudget"], 1000.0)
        self.assertEqual(s["stats"]["netRemaining"], 750.0)
        self.assertEqual(s["stats"]["totalEmployees"], 2)
        self.assertEqual(s["charts"]["budgetVsSpent"][0]["project"], "Alpha")
        self.assertGreaterEqual(s["activity"]["totalActions"], 1)
        self.assertEqual(self.ali_c.get("/api/reports/summary/").status_code, 403)

    def test_catalog_rows_and_persisted_rename_delete_duplicate(self):
        rows = self.a.get("/api/reports/catalog/?range=all").json()
        ids = {r["id"] for r in rows["results"]}
        self.assertIn(f"emp-{self.ali.id}", ids)
        self.assertIn(f"proj-{self.p.id}", ids)
        self.assertIn("agg-budget-vs-actual", ids)
        emp = next(r for r in rows["results"] if r["id"] == f"emp-{self.ali.id}")
        self.assertIn("1/2 tasks completed", emp["desc"])
        self.assertIn("1 action recorded", emp["desc"])

        key = f"proj-{self.p.id}"
        self.assertEqual(self.a.patch(f"/api/reports/catalog/{key}/", {"name": "Renamed"}, format="json").status_code, 200)
        after = self.a.get("/api/reports/catalog/?range=all").json()["results"]
        self.assertEqual(next(r for r in after if r["id"] == key)["name"], "Renamed")

        self.assertEqual(self.a.delete(f"/api/reports/catalog/{key}/").status_code, 204)
        self.assertNotIn(key, {r["id"] for r in self.a.get("/api/reports/catalog/?range=all").json()["results"]})
        self.assertTrue(ReportOverride.objects.get(key=key).hidden)

        dup = self.a.post("/api/reports/catalog/", {"name": "Copy", "category": "Project", "desc": "d"}, format="json")
        self.assertEqual(dup.status_code, 201)
        cid = dup.json()["id"]
        self.assertIn(cid, {r["id"] for r in self.a.get("/api/reports/catalog/?range=all").json()["results"]})
        self.assertEqual(self.a.delete(f"/api/reports/catalog/{cid}/").status_code, 204)
        self.assertFalse(CustomReport.objects.exists())

        self.assertEqual(self.a.patch("/api/reports/catalog/bogus-key/", {"name": "x"}, format="json").status_code, 404)
        self.assertEqual(self.a.post("/api/reports/catalog/", {"name": "x", "category": "Nope"}, format="json").status_code, 400)
        self.assertEqual(self.ali_c.get("/api/reports/catalog/").status_code, 403)
        self.assertEqual(self.ali_c.delete("/api/reports/catalog/agg-budget-vs-actual/").status_code, 403)


class DailyReportTests(ReportsTestBase):
    def upload(self, client, files=None, **data):
        payload = {"note": "Worked on login page", "project": "Alpha", **data}
        payload["files"] = files if files is not None else []
        return client.post("/api/reports/daily/", payload, format="multipart")

    def f(self, name, content, ctype="application/octet-stream"):
        return SimpleUploadedFile(name, content, content_type=ctype)

    def test_submit_with_photo_and_video_and_download(self):
        r = self.upload(self.ali_c, [self.f("a.jpg", JPEG), self.f("b.mp4", MP4)])
        self.assertEqual(r.status_code, 201, r.content)
        d = r.json()
        self.assertEqual(d["userName"], "Ali Khan")
        self.assertEqual(d["status"], "pending")
        self.assertEqual({x["kind"] for x in d["files"]}, {"image", "video"})
        url = d["files"][0]["url"]

        self.assertEqual(b"".join(self.ali_c.get(url).streaming_content), JPEG)   # owner
        self.assertEqual(self.a.get(url).status_code, 200)                         # admin
        self.assertEqual(self.sara_c.get(url).status_code, 404)                    # other employee
        self.assertEqual(APIClient().get(url).status_code, 401)                    # anonymous

        log = ActivityLog.objects.get(object_type="dailyreport", action="create")
        self.assertEqual(log.user, self.ali)
        self.assertEqual(log.metadata["files"], ["a.jpg", "b.mp4"])

    def test_rejects_disguised_and_dangerous_files(self):
        self.assertEqual(self.upload(self.ali_c, [self.f("evil.jpg", b"<?php system($_GET[1]); ?>" + b"x" * 40, "image/jpeg")]).status_code, 400)
        self.assertEqual(self.upload(self.ali_c, [self.f("x.svg", b"<svg onload=alert(1)></svg>", "image/svg+xml")]).status_code, 400)
        self.assertEqual(self.upload(self.ali_c, [self.f("e.png", b"")]).status_code, 400)
        self.assertFalse(DailyReport.objects.exists())

    def test_size_and_count_limits(self):
        import reports.views as v
        big = self.f("big.jpg", JPEG + b"0" * 2048)
        original = v.MAX_DAILY_FILE_MB
        v.MAX_DAILY_FILE_MB = 0  # any non-empty file is now "too large"
        try:
            self.assertEqual(self.upload(self.ali_c, [big]).status_code, 400)
        finally:
            v.MAX_DAILY_FILE_MB = original
        many = [self.f(f"{i}.jpg", JPEG) for i in range(11)]
        self.assertEqual(self.upload(self.ali_c, many).status_code, 400)

    def test_needs_note_or_file_and_no_future_date(self):
        self.assertEqual(self.ali_c.post("/api/reports/daily/", {"note": "", "project": "A"}, format="multipart").status_code, 400)
        future = (timezone.localdate() + timedelta(days=5)).isoformat()
        self.assertEqual(self.upload(self.ali_c, date=future).status_code, 400)

    def test_pending_account_cannot_submit(self):
        pending = make_user("p@x.com", status="pending")
        self.assertEqual(self.upload(client_for(pending)).status_code, 403)

    def test_visibility_and_filters(self):
        self.upload(self.ali_c, project="Alpha")
        self.upload(self.sara_c, project="Beta", note="Sara note")
        self.assertEqual(self.a.get("/api/reports/daily/").json()["count"], 2)
        self.assertEqual(self.ali_c.get("/api/reports/daily/").json()["count"], 1)
        self.assertEqual(self.a.get("/api/reports/daily/?project=Beta").json()["count"], 1)
        self.assertEqual(self.a.get(f"/api/reports/daily/?user={self.ali.id}").json()["count"], 1)
        self.assertEqual(self.a.get("/api/reports/daily/?q=sara").json()["count"], 1)
        self.assertEqual(self.a.get("/api/reports/daily/?status=approved").json()["count"], 0)

    def test_approve_is_admin_only_and_logged(self):
        rid = self.upload(self.ali_c).json()["id"]
        self.assertEqual(self.ali_c.post(f"/api/reports/daily/{rid}/approve/").status_code, 403)
        r = self.a.post(f"/api/reports/daily/{rid}/approve/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "approved")
        self.assertEqual(r.json()["approvedBy"], "Ada Admin")
        log = ActivityLog.objects.get(object_type="dailyreport", action="approve")
        self.assertEqual(log.user, self.admin)

    def test_delete_rules_and_files_removed_from_disk(self):
        rid = self.upload(self.ali_c, [self.f("a.jpg", JPEG)]).json()["id"]
        stored = DailyReportFile.objects.get(report_id=rid).file
        path = stored.path
        import os
        self.assertTrue(os.path.exists(path))
        self.assertEqual(self.sara_c.delete(f"/api/reports/daily/{rid}/").status_code, 404)  # not hers
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.ali_c.delete(f"/api/reports/daily/{rid}/").status_code, 204)
        self.assertFalse(os.path.exists(path))
        self.assertTrue(ActivityLog.objects.filter(object_type="dailyreport", action="delete", user=self.ali).exists())

    def test_bulk_delete_respects_ownership(self):
        a = self.upload(self.ali_c).json()["id"]
        s = self.upload(self.sara_c).json()["id"]
        r = self.ali_c.post("/api/reports/daily/bulk-delete/", {"ids": [a, s]}, format="json")
        self.assertEqual(r.json(), {"deleted": 1})
        self.assertTrue(DailyReport.objects.filter(pk=s).exists())
        self.assertEqual(self.a.post("/api/reports/daily/bulk-delete/", {"ids": [s]}, format="json").json(), {"deleted": 1})
        self.assertEqual(self.a.post("/api/reports/daily/bulk-delete/", {"ids": []}, format="json").status_code, 400)


class ClientRoleBlockedTests(ReportsTestBase):
    """Clients use the Client Portal, never Reports: every /api/reports/
    endpoint answers 403 for a "client" role token (even for own data or a
    non-existent id -- the role check runs before any lookup), while
    anonymous callers still get 401 and staff keep working normally."""

    ENDPOINTS = [
        ("get", "/api/reports/activity/"),
        ("get", "/api/reports/activity/filters/"),
        ("get", "/api/reports/activity/export/"),
        ("post", "/api/reports/activity/track/"),
        ("get", "/api/reports/users/"),
        ("get", "/api/reports/users/me/"),
        ("get", "/api/reports/users/1/"),
        ("get", "/api/reports/summary/"),
        ("get", "/api/reports/catalog/"),
        ("post", "/api/reports/catalog/"),
        ("patch", "/api/reports/catalog/emp-1/"),
        ("delete", "/api/reports/catalog/emp-1/"),
        ("get", "/api/reports/daily/"),
        ("post", "/api/reports/daily/"),
        ("post", "/api/reports/daily/bulk-delete/"),
        ("get", "/api/reports/daily/files/1/"),
        ("delete", "/api/reports/daily/1/"),
        ("post", "/api/reports/daily/1/approve/"),
    ]

    def setUp(self):
        super().setUp()
        self.client_user = make_user("client@x.com", "client", "Cli Ent")
        self.client_c = client_for(self.client_user)

    def test_client_role_gets_403_on_every_reports_endpoint(self):
        for method, url in self.ENDPOINTS:
            with self.subTest(method=method, url=url):
                resp = getattr(self.client_c, method)(url, {"note": "x"}, format="json")
                self.assertEqual(resp.status_code, 403, f"{method.upper()} {url} -> {resp.status_code}: {resp.content}")

    def test_client_cannot_submit_a_daily_report(self):
        resp = self.client_c.post("/api/reports/daily/", {"note": "hello"}, format="json")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(DailyReport.objects.count(), 0)

    def test_anonymous_still_gets_401(self):
        for method, url in self.ENDPOINTS:
            with self.subTest(method=method, url=url):
                resp = getattr(APIClient(), method)(url, {"note": "x"}, format="json")
                self.assertEqual(resp.status_code, 401, f"{method.upper()} {url} -> {resp.status_code}")

    def test_staff_roles_are_not_locked_out(self):
        self.assertEqual(self.ali_c.get("/api/reports/activity/").status_code, 200)
        self.assertEqual(self.ali_c.get("/api/reports/users/me/").status_code, 200)
        self.assertEqual(self.ali_c.post("/api/reports/daily/", {"note": "done"}, format="json").status_code, 201)
        self.assertEqual(self.a.get("/api/reports/summary/").status_code, 200)


class SniffTests(TestCase):
    def test_sniff(self):
        self.assertEqual(sniff(JPEG)[0], "image")
        self.assertEqual(sniff(PNG)[0], "image")
        self.assertEqual(sniff(MP4), ("video", "video/mp4"))
        self.assertEqual(sniff(b"\x00\x00\x00\x18ftypheic")[0], "image")
        self.assertIsNone(sniff(b"<svg xmlns=..."))
        self.assertIsNone(sniff(b"MZ\x90\x00"))


class FallbackMiddlewareTests(ReportsTestBase):
    """The safety net: API writes that touch no tracked model still leave a row."""

    def run_mw(self, method, path, user=None, status=200):
        from django.http import HttpResponse
        from django.test import RequestFactory
        from django.contrib.auth.models import AnonymousUser
        from .middleware import ActivityContextMiddleware

        req = getattr(RequestFactory(), method.lower())(path)
        req.user = user or AnonymousUser()
        ActivityContextMiddleware(lambda r: HttpResponse(status=status))(req)

    def test_untracked_write_is_recorded_with_module(self):
        self.run_mw("POST", "/api/sales/some-custom-action/", self.ali)
        log = ActivityLog.objects.get()
        self.assertEqual((log.action, log.module, log.user), ("api", "Sales", self.ali))
        self.assertIn("POST /api/sales/some-custom-action/", log.description)

    def test_download_is_recorded(self):
        self.run_mw("GET", "/api/tasks/tasks/4/zip/9/download/", self.ali)
        self.assertEqual(ActivityLog.objects.get().action, "download")

    def test_noise_reads_failures_anonymous_and_ignored_paths_are_skipped(self):
        self.run_mw("GET", "/api/tasks/tasks/", self.ali)                         # plain read
        self.run_mw("POST", "/api/tasks/x/", self.ali, status=400)                 # failed write
        self.run_mw("POST", "/api/dashboard/intake/", None)                        # anonymous / public form
        self.run_mw("POST", "/api/messages/thread/5/read/", self.ali)              # ignored: read receipts
        self.run_mw("POST", "/api/messages/calls/", self.ali)                      # ignored: call signalling
        self.run_mw("POST", "/not-api/x/", self.ali)
        self.assertEqual(ActivityLog.objects.count(), 0)

    def test_project_setting_can_ignore_more_paths(self):
        with self.settings(ACTIVITY_LOG_IGNORE_PATHS=[r"^/api/sales/noisy/"]):
            self.run_mw("POST", "/api/sales/noisy/", self.ali)
        self.assertEqual(ActivityLog.objects.count(), 0)


class AsgiAttributionTests(TestCase):
    """The project runs under Daphne (ASGI). Confirm the ContextVar carries the
    right user through the async -> sync hop, including concurrent requests."""

    async def test_async_client_attributes_correctly(self):
        from asgiref.sync import sync_to_async
        from django.test import AsyncClient

        ali = await sync_to_async(make_user)("ali@x.com", name="Ali Khan")
        sara = await sync_to_async(make_user)("sara@x.com", name="Sara Noor")
        toks = {u.pk: (await sync_to_async(Token.objects.create)(user=u)).key for u in (ali, sara)}
        await sync_to_async(ActivityLog.objects.all().delete)()

        async def create(user, title):
            r = await AsyncClient().post(
                "/api/tasks/tasks/", {"title": title, "project": "P"}, content_type="application/json",
                headers={"Authorization": f"Token {toks[user.pk]}"},
            )
            assert r.status_code == 201, r.content

        # Sequential on purpose: SQLite's in-memory test DB deadlocks under real
        # concurrency, but alternating users still proves no state leaks between requests.
        for i in range(10):
            await create(ali if i % 2 == 0 else sara, f"task-{i}")

        rows = await sync_to_async(lambda: list(ActivityLog.objects.filter(action="create", object_type="task")))()
        self.assertEqual(len(rows), 10)
        for row in rows:
            i = int(row.object_repr.split("-")[1])
            self.assertEqual(row.actor_email, "ali@x.com" if i % 2 == 0 else "sara@x.com", row.object_repr)