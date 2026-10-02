import io
import tempfile
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from PIL import Image
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from .models import CompanySettings, Department, NotificationPreference
from .notify import wants

User = get_user_model()


def png_bytes(size=(20, 20)):
    buf = io.BytesIO()
    Image.new("RGB", size, (120, 40, 200)).save(buf, "PNG")
    return buf.getvalue()


def make_user(email, role="employee", department="", status="approved", **extra):
    u = User.objects.create_user(email=email, password="pass12345", name=email.split("@")[0], role=role, department=department, status=status, **extra)
    return u


def auth(client, user):
    token, _ = Token.objects.get_or_create(user=user)
    client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")


class BaseCase(APITestCase):
    def setUp(self):
        self._media = tempfile.TemporaryDirectory()
        self.addCleanup(self._media.cleanup)
        self._override = override_settings(MEDIA_ROOT=self._media.name, COWORKING_PRIVATE_ROOT=self._media.name + "/private")
        self._override.enable()
        self.addCleanup(self._override.disable)
        # role "admin" WITHOUT is_staff — how admins are really created in the app
        self.admin = make_user("admin@x.com", role="admin", department="Management")
        self.emp = make_user("emp@x.com", role="employee", department="Design")


class LogoTests(BaseCase):
    def test_branding_is_public_and_defaults_empty(self):
        res = self.client.get("/api/settings/branding/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["logo"], "")

    def test_branding_ignores_a_stale_token(self):
        self.client.credentials(HTTP_AUTHORIZATION="Token not-a-real-token")
        self.assertEqual(self.client.get("/api/settings/branding/").status_code, 200)

    def test_admin_can_upload_and_it_shows_on_public_branding(self):
        auth(self.client, self.admin)
        f = SimpleUploadedFile("logo.png", png_bytes(), content_type="image/png")
        res = self.client.post("/api/settings/company/logo/", {"logo": f}, format="multipart")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertTrue(res.data["logo"].endswith(".png"))
        self.client.credentials()
        pub = self.client.get("/api/settings/branding/")
        self.assertEqual(pub.data["logo"], res.data["logo"])
        # company GET carries it too
        auth(self.client, self.admin)
        self.assertEqual(self.client.get("/api/settings/company/").data["logo"], res.data["logo"])

    def test_replacing_gets_a_new_url_and_removes_old_file(self):
        auth(self.client, self.admin)
        a = self.client.post("/api/settings/company/logo/", {"logo": SimpleUploadedFile("a.png", png_bytes())}, format="multipart")
        first = CompanySettings.load().logo.name
        b = self.client.post("/api/settings/company/logo/", {"logo": SimpleUploadedFile("b.png", png_bytes((30, 30)))}, format="multipart")
        self.assertNotEqual(a.data["logo"], b.data["logo"])
        self.assertFalse(CompanySettings.load().logo.storage.exists(first))

    def test_non_admin_refused(self):
        auth(self.client, self.emp)
        res = self.client.post("/api/settings/company/logo/", {"logo": SimpleUploadedFile("a.png", png_bytes())}, format="multipart")
        self.assertEqual(res.status_code, 403)

    def test_rejects_bad_files(self):
        auth(self.client, self.admin)
        url = "/api/settings/company/logo/"
        self.assertEqual(self.client.post(url, {"logo": SimpleUploadedFile("a.exe", b"MZ")}, format="multipart").status_code, 400)
        self.assertEqual(self.client.post(url, {"logo": SimpleUploadedFile("a.png", b"not an image")}, format="multipart").status_code, 400)
        big = SimpleUploadedFile("big.png", png_bytes() + b"0" * (2 * 1024 * 1024 + 10))
        self.assertEqual(self.client.post(url, {"logo": big}, format="multipart").status_code, 400)
        evil = SimpleUploadedFile("e.svg", b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>')
        self.assertEqual(self.client.post(url, {"logo": evil}, format="multipart").status_code, 400)
        self.assertEqual(self.client.post(url, {}, format="multipart").status_code, 400)

    def test_accepts_clean_svg_and_delete_resets(self):
        auth(self.client, self.admin)
        ok = SimpleUploadedFile("ok.svg", b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"><rect width="1" height="1"/></svg>')
        self.assertEqual(self.client.post("/api/settings/company/logo/", {"logo": ok}, format="multipart").status_code, 200)
        res = self.client.delete("/api/settings/company/logo/")
        self.assertEqual(res.data["logo"], "")
        self.assertEqual(self.client.get("/api/settings/branding/").data["logo"], "")

    def test_role_admin_can_now_save_company_settings(self):
        auth(self.client, self.admin)
        res = self.client.put("/api/settings/company/", {"name": "Acme"}, format="json")
        self.assertEqual(res.status_code, 200, res.data)


class DepartmentTests(BaseCase):
    def test_lists_the_real_departments_with_member_counts(self):
        make_user("d2@x.com", department="design")  # same dept, different case
        make_user("s@x.com", department="Sales")
        make_user("gone@x.com", department="Sales", status="deactivated")
        auth(self.client, self.emp)
        rows = {r["name"].lower(): r for r in self.client.get("/api/settings/departments/").data}
        self.assertEqual(set(rows), {"management", "design", "sales"})
        self.assertEqual(rows["design"]["members"], 2)
        self.assertEqual(rows["sales"]["members"], 1)  # deactivated user isn't a member
        self.assertEqual(rows["sales"]["total_users"], 2)
        # idempotent: second call doesn't duplicate
        self.assertEqual(len(self.client.get("/api/settings/departments/").data), 3)

    def test_admin_creates_edits_renames_and_deletes(self):
        auth(self.client, self.admin)
        res = self.client.post("/api/settings/departments/", {"name": "Legal", "budget": 5000, "head_id": self.admin.id}, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data["head"], self.admin.name)
        self.assertEqual(self.client.post("/api/settings/departments/", {"name": "legal"}, format="json").status_code, 400)

        design = Department.objects.filter(name__iexact="design").first() or None
        self.client.get("/api/settings/departments/")
        design = Department.objects.get(name__iexact="design")
        res = self.client.patch(f"/api/settings/departments/{design.id}/", {"name": "Creative", "budget": 900}, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        self.emp.refresh_from_db()
        self.assertEqual(self.emp.department, "Creative")  # rename cascades to the user

        # can't delete a department that still has people
        self.assertEqual(self.client.delete(f"/api/settings/departments/{design.id}/").status_code, 400)
        legal = Department.objects.get(name="Legal")
        self.assertEqual(self.client.delete(f"/api/settings/departments/{legal.id}/").status_code, 204)

    def test_employee_cannot_write(self):
        auth(self.client, self.emp)
        self.assertEqual(self.client.post("/api/settings/departments/", {"name": "X"}, format="json").status_code, 403)


class StorageTests(BaseCase):
    def test_reports_real_bytes_by_type(self):
        import os

        root = self._media.name
        os.makedirs(root + "/invoices", exist_ok=True)
        open(root + "/invoices/a.pdf", "wb").write(b"x" * 1000)
        open(root + "/b.png", "wb").write(b"x" * 500)
        open(root + "/c.zip", "wb").write(b"x" * 2000)
        open(root + "/d.weird", "wb").write(b"x" * 10)
        auth(self.client, self.admin)
        res = self.client.get("/api/settings/storage/?refresh=1")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["used_bytes"], 3510)
        self.assertEqual(res.data["file_count"], 4)
        by = {b["key"]: b for b in res.data["breakdown"]}
        self.assertEqual((by["documents"]["bytes"], by["images"]["bytes"], by["archives"]["bytes"], by["other"]["bytes"]), (1000, 500, 2000, 10))
        self.assertEqual(res.data["source"], "Server disk")
        self.assertGreater(res.data["limit_bytes"], 0)

    def test_admin_only(self):
        auth(self.client, self.emp)
        self.assertEqual(self.client.get("/api/settings/storage/").status_code, 403)

    def test_alert_when_nearly_full(self):
        from django.core.cache import cache
        from .models import BillingInfo

        cache.clear()
        b = BillingInfo.load()
        b.storage_limit_gb = "0.01"  # ~10 MB
        b.save()
        open(self._media.name + "/big.pdf", "wb").write(b"x" * 9_000_000)
        auth(self.client, self.admin)
        with mock.patch("settings.views.deliver") as d:
            self.client.get("/api/settings/storage/?refresh=1")
            self.client.get("/api/settings/storage/?refresh=1")
        self.assertEqual(d.call_count, 1)  # once a day, not on every refresh


class NotificationTests(BaseCase):
    def test_defaults_then_saved_toggles_are_enforced(self):
        self.assertTrue(wants(self.emp, "task_assigned", "push"))  # default
        auth(self.client, self.emp)
        rows = self.client.get("/api/settings/notifications/").data
        self.assertEqual([r["id"] for r in rows], ["task_assigned", "task_completed", "project_update", "invoice_paid", "new_message", "system_alerts"])
        for r in rows:
            if r["id"] == "task_assigned":
                r["push"] = False
                r["email"] = False
        self.client.put("/api/settings/notifications/", rows, format="json")
        self.assertFalse(wants(self.emp, "task_assigned", "push"))
        self.assertFalse(wants(self.emp, "task_assigned", "email"))

    def test_legacy_numeric_ids_do_not_create_junk_rows(self):
        auth(self.client, self.emp)
        self.client.put("/api/settings/notifications/", [{"id": 1, "label": "Task Assigned", "email": False, "push": False, "sms": False}], format="json")
        keys = set(NotificationPreference.objects.filter(user=self.emp).values_list("event_key", flat=True))
        self.assertNotIn("1", keys)
        self.assertFalse(wants(self.emp, "task_assigned", "push"))

    def test_legacy_numeric_row_is_moved_not_lost(self):
        NotificationPreference.objects.create(user=self.emp, event_key="4", label="Invoice Paid", email=False, push=False, sms=False)
        auth(self.client, self.emp)
        self.client.get("/api/settings/notifications/")
        self.assertFalse(NotificationPreference.objects.filter(user=self.emp, event_key="4").exists())
        self.assertFalse(wants(self.emp, "invoice_paid", "push"))  # the saved choice survived

    def test_push_is_skipped_when_user_turned_it_off(self):
        from messaging.push_utils import notify_user

        NotificationPreference.objects.create(user=self.emp, event_key="task_assigned", push=False, email=False)
        with mock.patch("messaging.push_utils.send_web_push") as sp, mock.patch("messaging.views.push_to_user") as ws:
            out = notify_user(self.emp, {"type": "task.assigned", "title": "t", "body": "b"}, email_subject="t")
        sp.assert_not_called()
        ws.assert_called_once()  # in-app dot still fires
        self.assertFalse(out["push"])
        self.assertFalse(out["email"])

    def test_push_and_email_fire_when_on(self):
        from django.core import mail
        from messaging.push_utils import notify_user

        with mock.patch("messaging.push_utils.send_web_push", return_value={"queued": True, "delivered": 0}) as sp, mock.patch("messaging.views.push_to_user"), \
                mock.patch("settings.notify.threading.Thread") as th:
            th.side_effect = lambda target, daemon=True: mock.Mock(start=target)
            out = notify_user(self.emp, {"type": "task.assigned", "title": "New task", "body": "hello"}, email_subject="New task")
        sp.assert_called_once()
        self.assertTrue(out["push"] and out["email"])
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["emp@x.com"])

    def test_company_email_switch_blocks_email(self):
        from messaging.push_utils import notify_user

        c = CompanySettings.load()
        c.email_notifications = False
        c.save()
        with mock.patch("messaging.push_utils.send_web_push", return_value={"queued": False, "delivered": 0}), mock.patch("messaging.views.push_to_user"):
            out = notify_user(self.emp, {"type": "task.assigned", "title": "t", "body": "b"}, email_subject="t")
        self.assertFalse(out["email"])

    def test_test_endpoint_reports_honestly(self):
        auth(self.client, self.emp)
        with mock.patch("messaging.views.push_to_user"):
            res = self.client.post("/api/settings/notifications/test/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["devices"], 0)
        self.assertIn("message", res.data)
        st = self.client.get("/api/settings/notifications/status/")
        self.assertEqual(st.status_code, 200)
        self.assertEqual(st.data["devices"], 0)

    def test_task_completed_and_invoice_paid_and_project_update_signals(self):
        from projects.models import Project
        from tasks.models import Task

        sent = []
        with mock.patch("settings.signals.deliver", side_effect=lambda u, p, e, **kw: sent.append((u.email, e))):
            t = Task.objects.create(title="T1", project="P", status="Pending")
            t.status = "Completed"
            t.save()
            t.title = "renamed"
            t.save()  # no status change -> no second notification
            p = Project.objects.create(name="Proj", manager=self.emp, created_by=self.admin)
            p.status = "Completed"
            p.save()
        self.assertIn(("admin@x.com", "task_completed"), sent)
        self.assertEqual(sum(1 for _u, e in sent if e == "task_completed"), 1)
        self.assertIn(("emp@x.com", "project_update"), sent)


class BillingTests(BaseCase):
    url = "/api/settings/billing/"

    def test_get_returns_catalog_and_empty_history(self):
        auth(self.client, self.emp)
        res = self.client.get(self.url)
        self.assertEqual(res.status_code, 200)
        self.assertEqual([p["name"] for p in res.data["plans"]], ["Starter", "Business", "Enterprise"])
        self.assertEqual(res.data["history"], [])

    def test_plan_change_is_priced_and_logged_by_the_server(self):
        auth(self.client, self.admin)
        res = self.client.put(self.url, {"plan_name": "business", "price": 1, "storage_limit_gb": 9999}, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["plan_name"], "Business")
        self.assertEqual(float(res.data["price"]), 24999.0)  # client-sent price ignored
        self.assertEqual(float(res.data["storage_limit_gb"]), 50.0)
        self.assertTrue(res.data["next_billing_date"])
        self.assertEqual(len(res.data["features"]), 5)
        self.assertEqual(res.data["history"][0]["description"], "Business Plan - Monthly")
        self.assertEqual(res.data["history"][0]["amount"], 24999.0)
        # same plan again -> no duplicate history row
        again = self.client.put(self.url, {"plan_name": "Business"}, format="json")
        self.assertEqual(len(again.data["history"]), 1)
        # storage bar now uses the plan's limit
        self.assertEqual(self.client.get("/api/settings/storage/?refresh=1").data["limit_bytes"], 50 * 1024**3)

    def test_unknown_plan_and_downgrade_below_usage_rejected(self):
        auth(self.client, self.admin)
        self.assertEqual(self.client.put(self.url, {"plan_name": "Platinum"}, format="json").status_code, 400)
        from django.core.cache import cache

        cache.clear()
        with mock.patch("settings.views.storage_usage.get_usage", return_value={"used_bytes": 20 * 1024**3}):
            res = self.client.put(self.url, {"plan_name": "Starter"}, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertIn("Free up space", res.data["error"])

    def test_card_update_stores_only_masked_data(self):
        auth(self.client, self.admin)
        res = self.client.put(self.url, {"card_last4": "4242", "card_brand": "Visa", "card_expiry": "12/45"}, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual((res.data["card_last4"], res.data["card_brand"]), ("4242", "Visa"))
        self.assertIn("Visa •••• 4242", res.data["history"][0]["description"])
        for bad in ({"card_last4": "42"}, {"card_last4": "4242", "card_expiry": "13/99"}, {"card_last4": "4242", "card_expiry": "01/20"}, {"card_last4": "abcd"}):
            self.assertEqual(self.client.put(self.url, bad, format="json").status_code, 400, bad)

    def test_only_admin_can_change(self):
        auth(self.client, self.emp)
        self.assertEqual(self.client.put(self.url, {"plan_name": "Starter"}, format="json").status_code, 403)


# ---------------------------------------------------------------------------
# Security: 2FA, sign-in history, logs, system info, danger zone
# ---------------------------------------------------------------------------
import time as _time

import pyotp


def current_code(secret):
    return pyotp.TOTP(secret).now()


def next_code(secret):
    """The code for the NEXT 30s step (so a test can log in twice in a row)."""
    return pyotp.TOTP(secret).at(int(_time.time()) + 30)


class TwoFactorTests(BaseCase):
    def enable(self):
        auth(self.client, self.admin)
        setup = self.client.post("/api/settings/security/2fa/setup/")
        self.assertEqual(setup.status_code, 200, setup.data)
        self.assertTrue(setup.data["qr"].startswith("data:image/svg+xml"))
        self.assertIn("otpauth://totp/", setup.data["uri"])
        res = self.client.post("/api/settings/security/2fa/enable/", {"code": current_code(setup.data["secret"])}, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(len(res.data["backup_codes"]), 8)
        return setup.data["secret"], res.data["backup_codes"]

    def test_flag_cannot_turn_2fa_on_or_off(self):
        auth(self.client, self.admin)
        self.client.put("/api/settings/security/", {"two_factor_enabled": True}, format="json")
        self.assertFalse(self.client.get("/api/settings/security/").data["two_factor_enabled"])

    def test_wrong_code_does_not_enable(self):
        auth(self.client, self.admin)
        self.client.post("/api/settings/security/2fa/setup/")
        res = self.client.post("/api/settings/security/2fa/enable/", {"code": "000000"}, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertFalse(self.client.get("/api/settings/security/").data["two_factor_enabled"])

    def test_login_needs_the_code_once_enabled(self):
        secret, backup = self.enable()
        self.client.credentials()
        # password only -> no token, asks for the code
        res = self.client.post("/api/auth/login/", {"email": "admin@x.com", "password": "pass12345"}, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data["otp_required"])
        self.assertNotIn("token", res.data)
        # wrong code
        bad = self.client.post("/api/auth/login/", {"email": "admin@x.com", "password": "pass12345", "otp": "123456"}, format="json")
        self.assertEqual(bad.status_code, 401)
        # right code (the setup code was already used, so use the next step's code)
        ok = self.client.post("/api/auth/login/", {"email": "admin@x.com", "password": "pass12345", "otp": next_code(secret)}, format="json")
        self.assertEqual(ok.status_code, 200, ok.data)
        self.assertIn("token", ok.data)
        # the same code can't be replayed
        again = self.client.post("/api/auth/login/", {"email": "admin@x.com", "password": "pass12345", "otp": next_code(secret)}, format="json")
        self.assertEqual(again.status_code, 401)

    def test_backup_code_works_exactly_once(self):
        _secret, backup = self.enable()
        self.client.credentials()
        body = {"email": "admin@x.com", "password": "pass12345", "otp": backup[0]}
        self.assertEqual(self.client.post("/api/auth/login/", body, format="json").status_code, 200)
        self.assertEqual(self.client.post("/api/auth/login/", body, format="json").status_code, 401)

    def test_google_login_also_needs_the_code(self):
        secret, _ = self.enable()
        self.client.credentials()
        fake = mock.Mock(status_code=200)
        fake.json.return_value = {"email": "admin@x.com"}
        with mock.patch("users.views.google_requests.get", return_value=fake):
            res = self.client.post("/api/auth/google-login/", {"access_token": "x"}, format="json")
            self.assertTrue(res.data.get("otp_required"))
            self.assertNotIn("token", res.data)
            ok = self.client.post("/api/auth/google-login/", {"access_token": "x", "otp": next_code(secret)}, format="json")
        self.assertIn("token", ok.data)

    def test_disable_needs_password_and_code(self):
        secret, _ = self.enable()
        url = "/api/settings/security/2fa/disable/"
        self.assertEqual(self.client.post(url, {"password": "nope", "code": next_code(secret)}, format="json").status_code, 400)
        self.assertEqual(self.client.post(url, {"password": "pass12345", "code": "000000"}, format="json").status_code, 400)
        ok = self.client.post(url, {"password": "pass12345", "code": next_code(secret)}, format="json")
        self.assertEqual(ok.status_code, 200)
        self.assertFalse(self.client.get("/api/settings/security/").data["two_factor_enabled"])
        # login is back to password only
        self.client.credentials()
        res = self.client.post("/api/auth/login/", {"email": "admin@x.com", "password": "pass12345"}, format="json")
        self.assertIn("token", res.data)

    def test_secret_is_not_stored_in_plain_text(self):
        secret, _ = self.enable()
        from .models import SecuritySetting

        self.assertNotIn(secret, SecuritySetting.objects.get(user=self.admin).totp_secret)


class SessionsLogsInfoTests(BaseCase):
    def login(self, email="admin@x.com"):
        self.client.credentials()
        res = self.client.post("/api/auth/login/", {"email": email, "password": "pass12345"}, format="json", HTTP_USER_AGENT="Mozilla/5.0 (Windows NT 10.0) Chrome/120.0 Safari/537.36")
        self.assertEqual(res.status_code, 200, res.data)
        return res.data["token"]

    def test_sessions_come_from_real_sign_ins(self):
        token = self.login()
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token}", HTTP_USER_AGENT="Mozilla/5.0 (Windows NT 10.0) Chrome/120.0 Safari/537.36")
        rows = self.client.get("/api/settings/security/sessions/").data["sessions"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["device"], "Chrome on Windows")
        self.assertTrue(rows[0]["current"])

    def test_sign_out_others_rotates_the_token(self):
        old = self.login()
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {old}")
        res = self.client.post("/api/settings/security/sessions/revoke-others/")
        self.assertEqual(res.status_code, 200)
        new = res.data["token"]
        self.assertNotEqual(old, new)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {old}")
        self.assertEqual(self.client.get("/api/settings/security/").status_code, 401)  # other devices are out
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {new}")
        self.assertEqual(self.client.get("/api/settings/security/").status_code, 200)  # this one keeps working

    def test_logs_are_real_and_admin_only(self):
        self.login()  # writes a "login" activity row
        self.client.credentials()
        self.client.post("/api/auth/login/", {"email": "admin@x.com", "password": "WRONG-pass1"}, format="json")  # failed login
        auth(self.client, self.admin)
        logs = self.client.get("/api/settings/logs/").data["logs"]
        self.assertTrue(any("Failed login" in l["message"] and l["level"] == "warning" for l in logs), logs)
        warn = self.client.get("/api/settings/logs/?level=warning").data["logs"]
        self.assertTrue(warn and all(l["level"] == "warning" for l in warn))
        info = self.client.get("/api/settings/logs/?level=info").data["logs"]
        self.assertTrue(all(l["level"] == "info" for l in info))
        auth(self.client, self.emp)
        self.assertEqual(self.client.get("/api/settings/logs/").status_code, 403)

    def test_system_info_is_measured(self):
        auth(self.client, self.admin)
        res = self.client.get("/api/settings/system-info/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["status"], "operational")
        self.assertTrue(res.data["database"]["ok"])
        self.assertIsNotNone(res.data["database"]["latency_ms"])
        self.assertFalse(res.data["backup"]["tracked"])  # no invented backup date

    def test_deletion_is_only_a_recorded_request(self):
        auth(self.client, self.admin)
        url = "/api/settings/account/delete-request/"
        self.assertEqual(self.client.post(url, {"password": "wrong"}, format="json").status_code, 400)
        with mock.patch("settings.views.deliver") as d:
            res = self.client.post(url, {"password": "pass12345"}, format="json")
        self.assertEqual(res.status_code, 201)
        self.assertTrue(self.client.get(url).data["requested"])
        self.assertTrue(User.objects.filter(pk=self.admin.pk).exists())  # nothing deleted
        self.assertFalse(self.client.delete(url).data["requested"])      # can be cancelled
        auth(self.client, self.emp)
        self.assertEqual(self.client.post(url, {"password": "pass12345"}, format="json").status_code, 403)

    def test_reset_restores_preferences_but_keeps_identity(self):
        auth(self.client, self.admin)
        self.client.put("/api/settings/company/", {"name": "Acme Ltd", "currency": "EUR"}, format="json")
        self.client.put("/api/settings/sales/", {"tax_rate": 17}, format="json")
        self.assertEqual(self.client.post("/api/settings/reset/").status_code, 200)
        self.assertEqual(float(self.client.get("/api/settings/sales/").data["tax_rate"]), 0.0)
        self.assertEqual(self.client.get("/api/settings/company/").data["name"], "Acme Ltd")
        self.assertNotEqual(self.client.get("/api/settings/company/").data["currency"], "EUR")
        auth(self.client, self.emp)
        self.assertEqual(self.client.post("/api/settings/reset/").status_code, 403)

    def test_app_config_for_any_user(self):
        auth(self.client, self.emp)
        res = self.client.get("/api/settings/app-config/")
        self.assertEqual(res.status_code, 200)
        for key in ("company", "projects", "tasks", "income", "expenses", "sales"):
            self.assertIn(key, res.data)


class SettingsAreEnforcedTests(BaseCase):
    def test_task_rules(self):
        from .models import TaskSettings

        auth(self.client, self.admin)
        base = {"title": "T", "project": "P"}
        self.assertEqual(self.client.post("/api/tasks/tasks/", base, format="json").status_code, 201)  # off by default
        cfg = TaskSettings.load()
        cfg.require_due_date = True
        cfg.allow_subtasks = False
        cfg.save()
        res = self.client.post("/api/tasks/tasks/", base, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertIn("dueDate", res.data)
        ok = self.client.post("/api/tasks/tasks/", {**base, "dueDate": "2030-01-01"}, format="json")
        self.assertEqual(ok.status_code, 201, ok.data)
        sub = self.client.post("/api/tasks/tasks/", {**base, "dueDate": "2030-01-01", "subtasks": [{"title": "a", "done": False}]}, format="json")
        self.assertEqual(sub.status_code, 400)
        self.assertIn("subtasks", sub.data)

    def test_auto_assign_lead(self):
        from projects.models import Project
        from .models import TaskSettings

        Project.objects.create(name="Alpha", manager=self.emp, created_by=self.admin)
        cfg = TaskSettings.load()
        cfg.auto_assign_lead = True
        cfg.save()
        auth(self.client, self.admin)
        res = self.client.post("/api/tasks/tasks/", {"title": "T", "project": "Alpha"}, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data["assignees"], [self.emp.name])
        explicit = self.client.post("/api/tasks/tasks/", {"title": "T2", "project": "Alpha", "assignees": ["Someone"]}, format="json")
        self.assertEqual(explicit.data["assignees"], ["Someone"])  # an explicit choice is never overridden

    def expense(self, amount, **extra):
        return self.client.post(
            "/api/expenses/expenses/",
            {"title": "Pens", "category": "Office", "project": "P", "amount": amount, "date": "2030-01-01", "payment": "Cash", **extra},
            format="json",
        )

    def test_expense_threshold_auto_approves_small_amounts(self):
        from .models import ExpenseSettings

        auth(self.client, self.admin)
        self.assertEqual(self.expense(100).data["status"], "Pending")  # off by default
        cfg = ExpenseSettings.load()
        cfg.approval_threshold = 1000
        cfg.require_receipt = False
        cfg.save()
        self.assertEqual(self.expense(500).data["status"], "Approved")
        self.assertEqual(self.expense(5000).data["status"], "Pending")  # above threshold needs a person

    def test_receipt_is_required_before_approval(self):
        from .models import ExpenseSettings

        cfg = ExpenseSettings.load()
        cfg.require_receipt = True
        cfg.approval_threshold = 1000
        cfg.save()
        auth(self.client, self.admin)
        created = self.expense(500)
        self.assertEqual(created.data["status"], "Pending")  # no receipt yet -> not auto-approved
        eid = created.data["id"]
        self.assertEqual(self.client.patch(f"/api/expenses/expenses/{eid}/", {"status": "Approved"}, format="json").status_code, 400)
        up = SimpleUploadedFile("r.png", png_bytes(), content_type="image/png")
        self.assertEqual(self.client.post(f"/api/expenses/expenses/{eid}/receipt/", {"receipt": up}, format="multipart").status_code, 201)
        self.assertEqual(self.client.get(f"/api/expenses/expenses/{eid}/").data["status"], "Approved")  # small + receipt -> auto-approved

    def test_auto_archive_completed_projects(self):
        from datetime import timedelta

        from django.utils import timezone
        from projects.models import Project
        from .rules import archive_old_completed_projects

        old = Project.objects.create(name="Old", status="Completed", created_by=self.admin)
        fresh = Project.objects.create(name="Fresh", status="Completed", created_by=self.admin)
        Project.objects.filter(pk=old.pk).update(updated_at=timezone.now() - timedelta(days=45))
        self.assertEqual(archive_old_completed_projects(), 1)
        old.refresh_from_db(); fresh.refresh_from_db()
        self.assertTrue(old.is_archived)
        self.assertFalse(fresh.is_archived)
        from .models import ProjectSettings

        cfg = ProjectSettings.load(); cfg.auto_archive = False; cfg.save()
        Project.objects.filter(pk=fresh.pk).update(updated_at=timezone.now() - timedelta(days=45))
        self.assertEqual(archive_old_completed_projects(), 0)  # switched off
