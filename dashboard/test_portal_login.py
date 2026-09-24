"""Client Portal login (Bug #2): Client ID + email alone used to be enough to
get a real auth token. It now also needs the portal password an admin
generated, is throttled, locks the account after repeated failures, and gives
one generic answer for every kind of credential failure.
"""

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from dashboard.models import Client

User = get_user_model()

URL = "/api/dashboard/clients/portal-login/"
PASSWORD = "Portal-Pass-123"


def post(api, data, ip="10.0.0.1"):
    return api.post(URL, data, format="json", REMOTE_ADDR=ip)


class PortalLoginTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.portal_user = User.objects.create_user(
            email="acme@client.test", password=PASSWORD, role="client",
            name="Acme Contact", status="approved",
        )
        cls.client_row = Client.objects.create(
            name="Acme Ltd", email="acme@client.test", portal_user=cls.portal_user,
        )
        # Same email, but an admin never generated portal access for it.
        cls.no_access = Client.objects.create(name="No Access Ltd", email="noaccess@client.test")

    def setUp(self):
        cache.clear()  # throttle counters live in the cache
        self.api = APIClient()

    def creds(self, **over):
        data = {"id": str(self.client_row.pk), "email": "acme@client.test", "password": PASSWORD}
        data.update(over)
        return data

    # -- the actual fix ---------------------------------------------------

    def test_correct_id_email_and_password_logs_in(self):
        r = post(self.api, self.creds())
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.data["token"], Token.objects.get(user=self.portal_user).key)
        self.assertEqual(r.data["client"]["id"], self.client_row.pk)

    def test_id_and_email_without_password_no_longer_works(self):
        """This is the original hole: ID + email alone returned a token."""
        r = post(self.api, {"id": str(self.client_row.pk), "email": "acme@client.test"})
        self.assertEqual(r.status_code, 400)
        self.assertNotIn("token", r.data)
        self.assertFalse(Token.objects.filter(user=self.portal_user).exists())

    def test_wrong_password_is_refused(self):
        r = post(self.api, self.creds(password="nope"))
        self.assertEqual(r.status_code, 401)
        self.assertNotIn("token", r.data)
        self.assertFalse(Token.objects.filter(user=self.portal_user).exists())

    def test_email_is_case_insensitive(self):
        r = post(self.api, self.creds(email="  ACME@Client.Test "))
        self.assertEqual(r.status_code, 200, r.content)

    # -- no account enumeration -------------------------------------------

    def test_every_credential_failure_gives_the_same_answer(self):
        cases = [
            self.creds(password="wrong"),                                  # wrong password
            self.creds(email="someone.else@client.test"),                  # wrong email
            self.creds(id="999999"),                                       # unknown Client ID
            self.creds(id="not-a-number"),                                 # junk Client ID
            self.creds(id="CLT-1024"),                                     # placeholder-style ID
            {"id": str(self.no_access.pk), "email": "noaccess@client.test", "password": PASSWORD},  # no portal access yet
        ]
        answers = set()
        for i, data in enumerate(cases):
            r = post(self.api, data, ip=f"10.0.1.{i}")
            self.assertEqual(r.status_code, 401, (data, r.content))
            answers.add(r.data["detail"])
        self.assertEqual(len(answers), 1, answers)

    # -- lockout ----------------------------------------------------------

    def test_account_locks_after_repeated_wrong_passwords(self):
        for i in range(User.LOCKOUT_THRESHOLD):
            r = post(self.api, self.creds(password="bad"), ip=f"10.0.2.{i}")
            self.assertEqual(r.status_code, 401)
        # Even the CORRECT password is refused while locked.
        r = post(self.api, self.creds(), ip="10.0.2.99")
        self.assertEqual(r.status_code, 423)
        self.assertNotIn("token", r.data)

    def test_successful_login_resets_failed_attempts(self):
        for i in range(2):
            post(self.api, self.creds(password="bad"), ip=f"10.0.3.{i}")
        self.portal_user.refresh_from_db()
        self.assertEqual(self.portal_user.failed_login_attempts, 2)
        r = post(self.api, self.creds(), ip="10.0.3.50")
        self.assertEqual(r.status_code, 200)
        self.portal_user.refresh_from_db()
        self.assertEqual(self.portal_user.failed_login_attempts, 0)

    # -- throttle ---------------------------------------------------------

    def test_ip_is_throttled_after_five_attempts_a_minute(self):
        codes = [post(self.api, self.creds(password="bad"), ip="10.0.4.1").status_code for _ in range(6)]
        self.assertEqual(codes[-1], 429, codes)

    # -- deactivated ------------------------------------------------------

    def test_deactivated_portal_user_is_refused_even_with_right_password(self):
        User.objects.filter(pk=self.portal_user.pk).update(status="deactivated")
        r = post(self.api, self.creds())
        self.assertEqual(r.status_code, 403)
        self.assertNotIn("token", r.data)

    def test_deactivated_portal_user_with_wrong_password_learns_nothing(self):
        User.objects.filter(pk=self.portal_user.pk).update(status="deactivated")
        r = post(self.api, self.creds(password="bad"))
        self.assertEqual(r.status_code, 401)

    # -- end to end with the admin "Generate Portal Access" flow ---------

    def test_password_from_generate_portal_access_actually_logs_in(self):
        admin = User.objects.create_user(
            email="boss@hopenix.test", password="admin-pass-123", role="admin",
            name="Boss", status="approved",
        )
        staff_api = APIClient()
        staff_api.credentials(HTTP_AUTHORIZATION=f"Token {Token.objects.get_or_create(user=admin)[0].key}")

        r = staff_api.post(f"/api/dashboard/clients/{self.no_access.pk}/portal-access/")
        self.assertEqual(r.status_code, 200, r.content)
        generated = r.data["password"]

        ok = post(self.api, {"id": str(self.no_access.pk), "email": "noaccess@client.test", "password": generated})
        self.assertEqual(ok.status_code, 200, ok.content)

        # And the token that comes back is a CLIENT token: staff-only
        # endpoints (Bug #1) still refuse it.
        client_api = APIClient()
        client_api.credentials(HTTP_AUTHORIZATION=f"Token {ok.data['token']}")
        self.assertEqual(client_api.get("/api/sales/sales/").status_code, 403)
