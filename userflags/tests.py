from django.contrib.auth import get_user_model
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient, APITestCase

from .models import UserFlag

User = get_user_model()


def api_for(user):
    token, _ = Token.objects.get_or_create(user=user)
    api = APIClient()
    api.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
    return api


class UserFlagTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.alice = User.objects.create_user(
            email="alice@flags.test", password="pw-12345", name="Alice", role="employee", status="approved"
        )
        cls.bob = User.objects.create_user(
            email="bob@flags.test", password="pw-12345", name="Bob", role="employee", status="approved"
        )

    def test_login_is_required(self):
        self.assertEqual(APIClient().get("/api/flags/").status_code, 401)
        self.assertEqual(APIClient().put("/api/flags/x/", {"value": 1}, format="json").status_code, 401)

    def test_flag_round_trips_and_is_visible_from_another_device(self):
        phone = api_for(self.alice)
        laptop = api_for(self.alice)  # same account, different "browser"
        payload = {"value": {"seen": ["t1", "t2"], "dot": True}}
        self.assertEqual(phone.put("/api/flags/tasks_seen/", payload, format="json").status_code, 200)
        flags = laptop.get("/api/flags/").json()["flags"]
        self.assertEqual(flags["tasks_seen"], {"seen": ["t1", "t2"], "dot": True})

    def test_put_again_replaces_instead_of_duplicating(self):
        api = api_for(self.alice)
        api.put("/api/flags/dot/", {"value": True}, format="json")
        api.put("/api/flags/dot/", {"value": False}, format="json")
        self.assertEqual(UserFlag.objects.filter(user=self.alice, key="dot").count(), 1)
        self.assertIs(api.get("/api/flags/").json()["flags"]["dot"], False)

    def test_flags_are_private_to_each_user(self):
        api_for(self.alice).put("/api/flags/dot/", {"value": True}, format="json")
        self.assertEqual(api_for(self.bob).get("/api/flags/").json()["flags"], {})

    def test_delete_removes_only_the_callers_flag(self):
        api_for(self.alice).put("/api/flags/dot/", {"value": 1}, format="json")
        api_for(self.bob).put("/api/flags/dot/", {"value": 2}, format="json")
        self.assertEqual(api_for(self.alice).delete("/api/flags/dot/").status_code, 204)
        self.assertFalse(UserFlag.objects.filter(user=self.alice, key="dot").exists())
        self.assertTrue(UserFlag.objects.filter(user=self.bob, key="dot").exists())

    def test_bad_key_missing_value_and_oversize_are_rejected(self):
        api = api_for(self.alice)
        self.assertEqual(api.put("/api/flags/bad key!/", {"value": 1}, format="json").status_code, 400)
        self.assertEqual(api.put("/api/flags/ok/", {"nope": 1}, format="json").status_code, 400)
        big = {"value": "x" * (70 * 1024)}
        self.assertEqual(api.put("/api/flags/ok/", big, format="json").status_code, 400)

    def test_flag_count_is_capped(self):
        api = api_for(self.alice)
        UserFlag.objects.bulk_create([UserFlag(user=self.alice, key=f"k{i}", value=1) for i in range(200)])
        self.assertEqual(api.put("/api/flags/one_more/", {"value": 1}, format="json").status_code, 400)
        # replacing an existing one is still allowed at the cap
        self.assertEqual(api.put("/api/flags/k0/", {"value": 2}, format="json").status_code, 200)
