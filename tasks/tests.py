from django.contrib.auth import get_user_model
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient, APITestCase

from .models import Task

User = get_user_model()


class TaskClientLinkFieldsTests(APITestCase):
    """moduleId / subModuleId / fromClientAssignment used to live only in one
    browser's localStorage. They must now survive a save + reload via the API,
    so the auto-task sync on another device can see the task already exists."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(
            email="admin@example.com", password="pw-12345", name="Admin", role="admin", status="approved"
        )

    def setUp(self):
        token, _ = Token.objects.get_or_create(user=self.admin)
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")

    def payload(self, **extra):
        return {
            "title": "Design homepage",
            "project": "Acme Site",
            "assignees": ["Admin"],
            "moduleTaskKey": "7::Acme Site::mod-0-1712345678::",
            **extra,
        }

    def test_link_fields_round_trip(self):
        response = self.client.post(
            "/api/tasks/tasks/",
            self.payload(moduleId="mod-0-1712345678", subModuleId="mod-0-sub-2", fromClientAssignment=True),
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertEqual(body["moduleId"], "mod-0-1712345678")
        self.assertEqual(body["subModuleId"], "mod-0-sub-2")
        self.assertIs(body["fromClientAssignment"], True)

        listed = self.client.get("/api/tasks/tasks/").json()
        row = next(t for t in (listed["results"] if isinstance(listed, dict) else listed) if t["id"] == body["id"])
        self.assertEqual(row["moduleId"], "mod-0-1712345678")
        self.assertEqual(row["subModuleId"], "mod-0-sub-2")
        self.assertIs(row["fromClientAssignment"], True)

    def test_null_sub_module_is_stored_as_empty_string(self):
        response = self.client.post(
            "/api/tasks/tasks/",
            self.payload(moduleId="mod-1", subModuleId=None),
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(Task.objects.get(pk=response.json()["id"]).sub_module_id, "")

    def test_omitting_the_fields_still_works(self):
        response = self.client.post("/api/tasks/tasks/", self.payload(), format="json")
        self.assertEqual(response.status_code, 201, response.content)
        task = Task.objects.get(pk=response.json()["id"])
        self.assertEqual((task.client_module_id, task.sub_module_id, task.from_client_assignment), ("", "", False))
