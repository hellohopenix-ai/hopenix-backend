import io
import json
import os
import shutil
import tempfile
from datetime import date
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from PIL import Image
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from dashboard.models import Income
from reports.models import ActivityLog

from .models import CoworkingApplication, CoworkingPayment

User = get_user_model()
LIST_URL = "/api/coworking/applications/"


def detail(code, suffix=""):
    return f"{LIST_URL}{code}/{suffix}"


def png_bytes(size=(24, 24), colour="white"):
    buf = io.BytesIO()
    Image.new("RGB", size, colour).save(buf, "PNG")
    return buf.getvalue()


def png(name="scan.png", pad=0):
    return SimpleUploadedFile(name, png_bytes() + b"\0" * pad, content_type="image/png")


def form(**overrides):
    """A valid submission, i.e. what CoworkingSpacePage's `form` state holds
    once every step passes validation (minus the three image fields, which
    travel as separate multipart file parts)."""
    data = {
        "formNo": "CWS-0001",
        "applicationDate": "2026-09-21",
        "agreementId": "",
        "startDate": "",
        "fullName": "Ayesha Khan",
        "fatherName": "Imran Khan",
        "cnic": "35202-1234567-1",
        "dob": "",
        "mobile": "0300-1234567",
        "whatsapp": "",
        "email": "ayesha@example.com",
        "address": "House 1, Street 2, Gujranwala",
        "applicantType": ["Company"],
        "companyName": "Khan Studio",
        "legalStatus": "",
        "registrationNo": "",
        "natureOfWork": "Design",
        "website": "",
        "teamSize": "",
        "chairs": 2,
        "ratePerChair": 25000,
        "duration": 3,
        "expectedEndDate": "",
        "seating": "Dedicated",
        "access": "Office Hours",
        "assignedChairs": "",
        "specialNotes": "",
        "members": [{"id": "abc-uuid", "name": "Ayesha Khan", "cnic": "", "phone": "", "chairNo": "C-01"}],
        "documents": ["NTN"],
        "emergencyName": "Imran Khan",
        "emergencyRelation": "Father",
        "emergencyPhone": "0301-7654321",
        "emergencyAltPhone": "",
        "discountPercent": 10,
        "securityDeposit": 20000,
        "otherCharges": 500,
        "paymentMethod": "Bank Transfer",
        "transactionNo": "",
        "billingDueDay": "5th",
        "billingCycle": "Monthly",
        "termsAgreed": True,
        "declarationName": "Ayesha Khan",
        "declarationDate": "2026-09-21",
        # The page also sends its own copy of the totals. They must be ignored.
        "subtotal": 1,
        "discountAmount": 1,
        "grandTotal": 1,
        "monthlyPayment": 1,
    }
    data.update(overrides)
    return data


class CoworkingTestCase(TestCase):
    @classmethod
    def setUpClass(cls):
        cls._private_root = tempfile.mkdtemp(prefix="cws_private_")
        cls._override = override_settings(COWORKING_PRIVATE_ROOT=cls._private_root)
        cls._override.enable()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls._override.disable()
        shutil.rmtree(cls._private_root, ignore_errors=True)

    @classmethod
    def setUpTestData(cls):
        def make(role):
            return User.objects.create_user(
                email=f"{role}@example.com", password="pw-12345", name=role.title(), role=role, status="approved"
            )

        cls.admin, cls.manager, cls.employee, cls.client_user = (
            make("admin"), make("manager"), make("employee"), make("client")
        )

    def api(self, user=None):
        client = APIClient()
        if user is not None:
            token, _ = Token.objects.get_or_create(user=user)
            client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        return client

    def submit(self, user=None, data=None, images=True, **overrides):
        body = {"data": json.dumps(data if data is not None else form(**overrides))}
        if images:
            body.update(cnicFront=png("front.png"), cnicBack=png("back.png"), photo=png("me.png"))
        return self.api(user or self.manager).post(LIST_URL, body, format="multipart")

    def make_application(self, **overrides):
        response = self.submit(**overrides)
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()

    def approve(self, code, **overrides):
        body = {"decision": "Approved", "approverRole": "Admin", "approvedBy": "Boss", **overrides}
        return self.api(self.admin).post(detail(code, "review/"), body, format="json")


# ==========================================================================
class SubmitTests(CoworkingTestCase):
    def test_manager_submits_and_gets_back_a_record_shaped_like_the_page_state(self):
        response = self.submit()
        self.assertEqual(response.status_code, 201, response.content)
        record = response.json()

        # Postgres sequences survive test rollbacks, so compare with the row's own pk.
        expected = f"CWS-{CoworkingApplication.objects.get().pk:04d}"
        self.assertEqual(record["id"], expected)
        self.assertEqual(record["status"], "Pending")
        self.assertIsNone(record["review"])
        self.assertEqual(record["payments"], [])
        self.assertEqual(record["application"]["formNo"], expected)  # the client's own "CWS-0001" is ignored
        self.assertEqual(record["application"]["members"][0]["chairNo"], "C-01")
        self.assertEqual(record["application"]["applicantType"], ["Company"])
        self.assertEqual(record["createdBy"], "Manager")
        # empty optional values come back as "" (never null) for React inputs
        self.assertEqual(record["application"]["startDate"], "")
        self.assertEqual(record["application"]["teamSize"], "")

    def test_totals_are_computed_by_the_server_not_trusted_from_the_client(self):
        app = self.make_application()["application"]
        # 2 chairs x 25,000 = 50,000/month; x3 months = 150,000
        self.assertEqual(app["monthlyPayment"], 50000)
        self.assertEqual(app["subtotal"], 150000)
        self.assertEqual(app["discountAmount"], 15000)  # 10%
        self.assertEqual(app["grandTotal"], 155500)  # 150,000 - 15,000 + 20,000 deposit + 500 other

    def test_numbers_are_json_numbers_not_strings(self):
        app = self.make_application(chairs="2", ratePerChair="25000")["application"]  # form inputs are strings
        self.assertIsInstance(app["monthlyPayment"], int)
        self.assertIsInstance(app["chairs"], int)

    def test_blank_optional_dates_numbers_and_money_are_accepted(self):
        response = self.submit(
            startDate="", dob="", expectedEndDate="", teamSize="", discountPercent="", securityDeposit="", otherCharges=""
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["application"]["grandTotal"], 150000)

    def test_images_are_stored_privately_and_never_under_media_root(self):
        self.make_application()
        app = CoworkingApplication.objects.get()
        for field in (app.photo, app.cnic_front, app.cnic_back):
            self.assertTrue(field.storage.exists(field.name))
            self.assertTrue(os.path.abspath(field.path).startswith(os.path.abspath(self._private_root)))
            self.assertFalse(os.path.abspath(field.path).startswith(os.path.abspath(settings.MEDIA_ROOT)))
            self.assertNotIn("scan", field.name)  # original filename (PII) isn't kept

    def test_missing_cnic_images_are_rejected_with_the_pages_own_wording(self):
        response = self.submit(images=False)
        self.assertEqual(response.status_code, 400)
        errors = response.json()
        self.assertEqual(errors["cnicFront"], ["Upload the front of your CNIC"])
        self.assertEqual(errors["cnicBack"], ["Upload the back of your CNIC"])
        self.assertEqual(CoworkingApplication.objects.count(), 0)

    def test_image_parts_need_a_real_file_extension(self):
        # The page names each part from the image's MIME type (front.png / .jpg ...).
        # An extensionless filename is refused by Django's ImageField, so the page must never send one.
        body = {"data": json.dumps(form()), "cnicFront": png("cnic-front"), "cnicBack": png("cnic-back.png")}
        response = self.api(self.manager).post(LIST_URL, body, format="multipart")
        self.assertEqual(response.status_code, 400)
        self.assertIn("cnicFront", response.json())
        self.assertNotIn("cnicBack", response.json())

    def test_photo_is_optional(self):
        body = {"data": json.dumps(form()), "cnicFront": png(), "cnicBack": png()}
        response = self.api(self.manager).post(LIST_URL, body, format="multipart")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertIsNone(response.json()["application"]["photoUrl"])

    def test_non_image_and_oversized_uploads_are_rejected(self):
        fake = SimpleUploadedFile("front.png", b"this is not an image", content_type="image/png")
        body = {"data": json.dumps(form()), "cnicFront": fake, "cnicBack": png()}
        response = self.api(self.manager).post(LIST_URL, body, format="multipart")
        self.assertEqual(response.status_code, 400)
        self.assertIn("cnicFront", response.json())

        huge = png("front.png", pad=5 * 1024 * 1024)  # a real PNG, just over the 5MB limit
        body = {"data": json.dumps(form()), "cnicFront": huge, "cnicBack": png()}
        response = self.api(self.manager).post(LIST_URL, body, format="multipart")
        self.assertEqual(response.status_code, 400)
        self.assertIn("5MB", response.json()["cnicFront"][0])

    def test_validation_mirrors_the_page_and_every_error_is_a_flat_string_list(self):
        bad = form(
            fullName="",
            cnic="123",
            mobile="12345",
            whatsapp="nope",
            email="not-an-email",
            address="",
            applicantType=[],
            website="example.com",
            teamSize=0,
            chairs=13,
            ratePerChair=0,
            duration=0,
            members=[{"name": "", "cnic": "bad", "phone": "bad", "chairNo": ""}],
            emergencyName="",
            emergencyPhone="",
            termsAgreed=False,
            declarationName="",
        )
        response = self.submit(data=bad)
        self.assertEqual(response.status_code, 400)
        errors = response.json()
        for field in (
            "fullName", "cnic", "mobile", "whatsapp", "email", "address", "applicantType", "website", "teamSize",
            "chairs", "ratePerChair", "duration", "members", "emergencyName", "emergencyPhone", "termsAgreed",
            "declarationName",
        ):
            self.assertIn(field, errors, field)
        # flat: {field: [str, ...]} -- nothing nested (the page joins these into one toast)
        for field, messages in errors.items():
            self.assertIsInstance(messages, list, field)
            self.assertTrue(all(isinstance(m, str) for m in messages), field)
        self.assertEqual(errors["chairs"], ["Only 12 chairs total are available"])
        self.assertEqual(errors["cnic"], ["Use CNIC format 12345-1234567-1, or a valid passport number"])
        self.assertIn("Member 1: name required", errors["members"])

    def test_company_or_agency_needs_a_company_name(self):
        for kind in ("Company", "Agency"):
            response = self.submit(applicantType=[kind], companyName=" ")
            self.assertEqual(response.status_code, 400)
            self.assertIn("companyName", response.json())
        self.assertEqual(self.submit(applicantType=["Freelancer"], companyName="").status_code, 201)

    def test_passport_numbers_and_plus92_phones_are_accepted(self):
        self.assertEqual(self.submit(cnic="AB1234567", mobile="+923001234567").status_code, 201)

    def test_blank_member_rows_are_ignored(self):
        members = [
            {"name": "Ayesha Khan", "cnic": "", "phone": "", "chairNo": ""},
            {"name": "", "cnic": "", "phone": "", "chairNo": ""},
        ]
        record = self.make_application(members=members)
        self.assertEqual(len(record["application"]["members"]), 1)

    def test_form_numbers_are_sequential_and_never_reused_after_a_delete(self):
        number = lambda code: int(code.split("-")[1])  # noqa: E731
        first = self.make_application()["id"]
        second = self.make_application()["id"]
        self.assertEqual(number(second), number(first) + 1)
        self.assertEqual(self.api(self.admin).delete(detail(second)).status_code, 204)
        third = self.make_application()["id"]
        self.assertEqual(number(third), number(second) + 1)  # the deleted number is NOT handed out again
        self.assertNotEqual(third, second)

    def test_a_json_body_without_files_is_a_clean_400_not_a_crash(self):
        response = self.api(self.manager).post(LIST_URL, form(), format="json")
        self.assertEqual(response.status_code, 400)
        response = self.api(self.manager).post(LIST_URL, {"data": "{not json"}, format="multipart")
        self.assertEqual(response.status_code, 400)
        response = self.api(self.manager).post(LIST_URL, {"data": "[1,2]"}, format="multipart")
        self.assertEqual(response.status_code, 400)

    def test_admins_are_notified_without_personal_details(self):
        payloads = []
        with mock.patch("messaging.consumers.is_user_online", return_value=True), mock.patch(
            "messaging.views.push_to_user", side_effect=lambda uid, p: payloads.append((uid, p))
        ):
            self.make_application()  # submitted by a manager
        self.assertEqual([uid for uid, _ in payloads], [self.admin.id])
        payload = payloads[0][1]
        self.assertEqual(payload["type"], "coworking.application")
        blob = json.dumps(payload)
        for secret in ("35202", "0300-1234567", "House 1", "ayesha@example.com"):
            self.assertNotIn(secret, blob)

    def test_a_broken_notification_never_fails_the_submission(self):
        with mock.patch("messaging.consumers.is_user_online", side_effect=RuntimeError("boom")):
            with self.assertLogs("coworking.views", level="ERROR"):
                self.assertEqual(self.submit().status_code, 201)


# ==========================================================================
class PermissionTests(CoworkingTestCase):
    def test_only_admin_and_manager_can_touch_anything(self):
        code = self.make_application()["id"]
        for user in (self.employee, self.client_user, None):
            api = self.api(user)
            for response in (
                api.get(LIST_URL),
                api.get(detail(code)),
                self.submit(user=user) if user else api.post(LIST_URL, {}, format="json"),
                api.post(detail(code, "payments/"), {"month": "2026-09", "amount": 1}, format="json"),
                api.get(detail(code, "files/cnic-front/")),
            ):
                self.assertIn(response.status_code, (401, 403), (user, response.status_code))

    def test_manager_can_list_and_view(self):
        code = self.make_application()["id"]
        api = self.api(self.manager)
        self.assertEqual([r["id"] for r in api.get(LIST_URL).json()], [code])
        self.assertEqual(api.get(detail(code)).json()["id"], code)

    def test_manager_cannot_review_or_delete_but_admin_can(self):
        code = self.make_application()["id"]
        manager = self.api(self.manager)
        self.assertEqual(manager.post(detail(code, "review/"), {"decision": "Approved"}, format="json").status_code, 403)
        self.assertEqual(manager.delete(detail(code)).status_code, 403)
        self.assertEqual(CoworkingApplication.objects.get().status, "Pending")

        self.assertEqual(self.approve(code).status_code, 200)
        self.assertEqual(self.api(self.admin).delete(detail(code)).status_code, 204)
        self.assertEqual(CoworkingApplication.objects.count(), 0)

    def test_there_is_no_update_endpoint(self):
        code = self.make_application()["id"]
        self.assertEqual(self.api(self.admin).patch(detail(code), {"chairs": 1}, format="json").status_code, 405)


# ==========================================================================
class ReviewTests(CoworkingTestCase):
    def test_approve_creates_the_review_and_moves_the_status(self):
        code = self.make_application()["id"]
        response = self.approve(
            code, approvedChairs=2, approvedRate=25000, discount=15000, deposit=20000, approvedGrandTotal=155500,
            chairNos="C-01, C-02", accessCardNo="A-17", wifiIssued=True, effectiveFrom="2026-10-01", remarks="ok",
        )
        self.assertEqual(response.status_code, 200, response.content)
        record = response.json()
        self.assertEqual(record["status"], "Approved")
        self.assertEqual(record["review"]["decision"], "Approved")
        self.assertEqual(record["review"]["chairNos"], "C-01, C-02")
        self.assertTrue(record["review"]["wifiIssued"])
        self.assertEqual(record["review"]["reviewedBy"], "Admin")
        # billing calendar now starts from the review's effectiveFrom
        self.assertEqual(record["billingMonths"], ["2026-10", "2026-11", "2026-12"])

    def test_the_review_panels_blank_fields_and_echoed_keys_are_accepted(self):
        code = self.make_application()["id"]
        response = self.approve(
            code, approvalDate="", effectiveFrom="", approvedChairs="", approvedRate="", discount="", deposit="",
            approvedGrandTotal="", reviewedBy="ignored", reviewedAt="ignored",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["review"]["approvedChairs"], "")

    def test_saving_again_updates_the_same_review_and_can_reject_or_hold(self):
        code = self.make_application()["id"]
        self.approve(code, remarks="first")
        self.approve(code, remarks="second")
        self.assertEqual(CoworkingApplication.objects.get().review.remarks, "second")
        for decision in ("Rejected", "Pending"):
            record = self.approve(code, decision=decision).json()
            self.assertEqual(record["status"], decision)
            self.assertEqual(record["review"]["decision"], decision)

    def test_invalid_decision_is_rejected(self):
        code = self.make_application()["id"]
        self.assertEqual(self.approve(code, decision="Maybe").status_code, 400)

    def test_cannot_approve_more_chairs_than_are_free(self):
        big = self.make_application(chairs=8)["id"]
        other = self.make_application(chairs=5)["id"]
        self.assertEqual(self.approve(big).status_code, 200)

        response = self.approve(other)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["decision"], [f"Not enough free chairs: {other} needs 5, but only 4 of 12 are free."]
        )
        self.assertEqual(CoworkingApplication.objects.get(code=other).status, "Pending")

        # Rejecting / holding never needs a free chair, and it frees the seats up again
        self.assertEqual(self.approve(other, decision="Rejected").status_code, 200)
        self.assertEqual(self.approve(big, decision="Rejected").status_code, 200)
        self.assertEqual(self.approve(other).status_code, 200)

    def test_re_approving_an_application_does_not_count_its_own_chairs_twice(self):
        code = self.make_application(chairs=12)["id"]
        self.assertEqual(self.approve(code).status_code, 200)
        self.assertEqual(self.approve(code, remarks="edited").status_code, 200)

    @override_settings(COWORKING_TOTAL_CHAIRS=3)
    def test_capacity_is_configurable(self):
        self.assertEqual(self.submit(chairs=4).status_code, 400)
        self.assertEqual(self.submit(chairs=3).status_code, 201)

    def test_approval_is_recorded_on_the_reports_activity_log(self):
        code = self.make_application()["id"]
        self.approve(code)
        rows = ActivityLog.objects.filter(module="Coworking")
        self.assertTrue(rows.filter(action="create").exists())
        approval = rows.get(action="approve")
        self.assertIn(code, approval.description)
        self.assertEqual(approval.user_id, self.admin.id)
        # a national ID number never lands in the log
        self.assertFalse(any("35202" in (r.description + json.dumps(r.changes) + json.dumps(r.metadata)) for r in rows))


# ==========================================================================
class PaymentTests(CoworkingTestCase):
    def approved(self, **overrides):
        code = self.make_application(**overrides)["id"]
        self.assertEqual(self.approve(code, effectiveFrom="2026-09-01").status_code, 200)
        return code

    def tick(self, code, month="2026-09", amount=50000, user=None, **extra):
        return self.api(user or self.manager).post(
            detail(code, "payments/"), {"month": month, "amount": amount, **extra}, format="json"
        )

    def test_payments_need_an_approved_application(self):
        code = self.make_application()["id"]
        response = self.tick(code)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], ["Waiting for admin approval before payment can be recorded."])
        self.assertEqual(Income.objects.count(), 0)

    def test_a_tick_creates_the_payment_and_the_income_ledger_row_together(self):
        code = self.approved()
        response = self.tick(code)
        self.assertEqual(response.status_code, 201, response.content)

        income = Income.objects.get()
        self.assertEqual(income.description, f"Coworking Space — {code} — September 2026")
        self.assertEqual(income.project, "Coworking Space")
        self.assertEqual(income.client, "Khan Studio")
        self.assertEqual(income.amount, 50000)
        self.assertEqual(income.status, "Received")
        self.assertEqual(income.method, "Bank Transfer")
        self.assertEqual(income.created_by, self.manager)

        record = response.json()
        self.assertEqual(len(record["payments"]), 1)
        self.assertEqual(record["payments"][0]["month"], "2026-09")
        self.assertEqual(record["payments"][0]["amount"], 50000)
        self.assertEqual(CoworkingPayment.objects.get().income_id, income.id)

    def test_client_name_falls_back_to_the_applicant_and_is_clipped_to_incomes_limit(self):
        code = self.approved(applicantType=["Freelancer"], companyName="", fullName="A" * 200)
        self.assertEqual(self.tick(code).status_code, 201)
        self.assertEqual(len(Income.objects.get().client), 150)

    def test_cash_and_card_are_translated_to_methods_the_income_ledger_accepts(self):
        # The page offers Cash + Card, the Income model does not -- posting them raw would 400.
        cases = {"Cash": "Cash in Hand", "Card": "Bank Transfer", "JazzCash": "JazzCash", "Easypaisa": "Easypaisa"}
        for method, expected in cases.items():
            Income.objects.all().delete()
            code = self.approved(paymentMethod=method)
            self.assertEqual(self.tick(code).status_code, 201, method)
            self.assertEqual(Income.objects.get().method, expected, method)

    def test_a_month_can_be_paid_in_parts_but_never_over_the_monthly_amount(self):
        code = self.approved()
        self.assertEqual(self.tick(code, amount=20000).status_code, 201)

        over = self.tick(code, amount=40000)
        self.assertEqual(over.status_code, 400)
        self.assertEqual(over.json()["amount"], ["Only PKR 30,000 is due for September 2026."])

        self.assertEqual(self.tick(code, amount=30000).status_code, 201)
        again = self.tick(code, amount=1)
        self.assertEqual(again.status_code, 400)
        self.assertEqual(again.json()["amount"], ["September 2026 is already fully paid."])
        self.assertEqual(Income.objects.count(), 2)
        self.assertEqual(sum(i.amount for i in Income.objects.all()), 50000)

    def test_only_months_in_the_billing_period_can_be_paid(self):
        code = self.approved()  # 3 months from 2026-09
        for month in ("2026-08", "2026-12"):
            response = self.tick(code, month=month)
            self.assertEqual(response.status_code, 400, month)
            self.assertIn("outside this rental's billing period", response.json()["month"][0])
        self.assertEqual(self.tick(code, month="2026-11").status_code, 201)

    def test_bad_amounts_and_months_are_rejected(self):
        code = self.approved()
        for body in ({"month": "2026-09", "amount": 0}, {"month": "2026-09", "amount": -5},
                     {"month": "2026-09", "amount": "abc"}, {"month": "Sep 2026", "amount": 100}, {"amount": 100}):
            response = self.api(self.manager).post(detail(code, "payments/"), body, format="json")
            self.assertEqual(response.status_code, 400, body)
        self.assertEqual(Income.objects.count(), 0)

    def test_billing_months_roll_over_the_year_end(self):
        code = self.make_application(startDate="2026-11-15", duration=4)["id"]
        record = self.api(self.manager).get(detail(code)).json()
        self.assertEqual(record["billingMonths"], ["2026-11", "2026-12", "2027-01", "2027-02"])

    def test_billing_starts_from_the_submission_month_when_no_dates_are_given(self):
        record = self.make_application(startDate="", duration=2)
        months = record["billingMonths"]
        today = date.today()
        self.assertEqual(months[0], f"{today.year}-{today.month:02d}")
        self.assertEqual(len(months), 2)

    def test_deleting_the_income_entry_reopens_the_month(self):
        code = self.approved()
        self.tick(code)
        Income.objects.get().delete()  # e.g. fixed on the Income page
        self.assertEqual(CoworkingPayment.objects.count(), 0)
        self.assertEqual(self.api(self.manager).get(detail(code)).json()["payments"], [])
        self.assertEqual(self.tick(code).status_code, 201)

    def test_deleting_an_application_keeps_the_income_that_was_received(self):
        code = self.approved()
        self.tick(code)
        self.assertEqual(self.api(self.admin).delete(detail(code)).status_code, 204)
        self.assertEqual(CoworkingPayment.objects.count(), 0)
        self.assertEqual(Income.objects.count(), 1)

    def test_income_creation_is_on_the_activity_log_under_the_person_who_ticked(self):
        code = self.approved()
        self.tick(code)
        row = ActivityLog.objects.get(module="Income", action="create")
        self.assertEqual(row.user_id, self.manager.id)
        self.assertIn(code, row.description)


# ==========================================================================
class FileTests(CoworkingTestCase):
    def test_staff_can_fetch_an_id_scan_and_it_is_audited(self):
        code = self.make_application()["id"]
        record = self.api(self.manager).get(detail(code)).json()["application"]
        self.assertTrue(record["cnicFrontUrl"].endswith(f"/api/coworking/applications/{code}/files/cnic-front/"))

        response = self.api(self.manager).get(detail(code, "files/cnic-front/"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/png")
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertEqual(b"".join(response.streaming_content), png_bytes())

        log = ActivityLog.objects.get(module="Coworking", action="view")
        self.assertEqual(log.user_id, self.manager.id)
        self.assertIn("cnic front", log.description)

    def test_missing_file_and_unknown_kind_are_404(self):
        body = {"data": json.dumps(form()), "cnicFront": png(), "cnicBack": png()}  # no photo
        code = self.api(self.manager).post(LIST_URL, body, format="multipart").json()["id"]
        self.assertEqual(self.api(self.manager).get(detail(code, "files/photo/")).status_code, 404)
        self.assertEqual(self.api(self.manager).get(detail(code, "files/passport/")).status_code, 404)

    def test_deleting_an_application_removes_its_files_from_disk(self):
        code = self.make_application()["id"]
        app = CoworkingApplication.objects.get()
        paths = [f.path for f in (app.photo, app.cnic_front, app.cnic_back)]
        self.assertTrue(all(os.path.exists(p) for p in paths))
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.api(self.admin).delete(detail(code)).status_code, 204)
        self.assertFalse(any(os.path.exists(p) for p in paths))


class AdminSiteTests(CoworkingTestCase):
    def test_django_admin_pages_render_despite_the_private_file_storage(self):
        code = self.make_application()["id"]
        self.assertEqual(self.approve(code).status_code, 200)
        root = User.objects.create_superuser(email="root@example.com", password="pw-12345", name="Root")
        client = self.client
        client.force_login(root)
        app = CoworkingApplication.objects.get()
        self.assertEqual(client.get("/admin/coworking/coworkingapplication/").status_code, 200)
        self.assertEqual(client.get(f"/admin/coworking/coworkingapplication/{app.pk}/change/").status_code, 200)
