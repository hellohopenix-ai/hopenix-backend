"""Server-side enforcement checks for users/access.py (issue #1).

Confirms two things at once for every staff module (Sales, Expenses,
Visitors, Tasks) plus the Employees directory:

  * a "client" role token is refused (they use the Client Portal, never
    these staff-only list endpoints) -- this used to be a hole: any token,
    including a client's, could call these APIs directly and skip the UI.
  * roles that ARE allowed in (per users.access.DEFAULT_ROLE_PAGES /
    DEFAULT_MODULE_FLAGS -- the same defaults AuthContext.jsx ships with)
    keep working normally, so the new enforcement doesn't lock out
    legitimate use.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

User = get_user_model()

# (url, roles that should be refused, roles that should be let through)
LIST_ENDPOINTS = [
    ("/api/sales/sales/", ["client", "employee"], ["manager", "admin"]),
    ("/api/expenses/expenses/", ["client", "employee"], ["manager", "admin"]),
    ("/api/visitors/visitors/", ["client", "employee"], ["manager", "admin"]),
    ("/api/tasks/tasks/", ["client"], ["employee", "manager", "admin"]),
]


def make_client(role):
    user = User.objects.create_user(
        email=f"{role}@moduleaccess.test",
        password="pass12345",
        role=role,
        name=role.title(),
        status="approved",
    )
    token, _ = Token.objects.get_or_create(user=user)
    api = APIClient()
    api.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
    return api


class ModuleAccessEnforcementTests(TestCase):
    """One user per role, reused across every endpoint in the table above."""

    @classmethod
    def setUpTestData(cls):
        cls.clients_by_role = {
            role: make_client(role) for role in ("client", "employee", "manager", "admin")
        }

    def test_refused_roles_get_403(self):
        for url, refused_roles, _ in LIST_ENDPOINTS:
            for role in refused_roles:
                with self.subTest(url=url, role=role):
                    resp = self.clients_by_role[role].get(url)
                    self.assertEqual(
                        resp.status_code, 403,
                        f"{role} should NOT reach {url} (got {resp.status_code}): {resp.content}",
                    )

    def test_allowed_roles_get_200(self):
        for url, _, allowed_roles in LIST_ENDPOINTS:
            for role in allowed_roles:
                with self.subTest(url=url, role=role):
                    resp = self.clients_by_role[role].get(url)
                    self.assertEqual(
                        resp.status_code, 200,
                        f"{role} SHOULD reach {url} (got {resp.status_code}): {resp.content}",
                    )

    def test_employee_directory_blocks_only_the_client_role(self):
        # Deliberately different rule from the modules above: the
        # Employees list is a shared staff directory (see employees/
        # permissions.IsStaff) -- employee/manager/admin all see it,
        # only "client" is refused.
        for role, expected in [("client", 403), ("employee", 200), ("manager", 200), ("admin", 200)]:
            with self.subTest(role=role):
                resp = self.clients_by_role[role].get("/api/employees/")
                self.assertEqual(resp.status_code, expected, resp.content)

    def test_intake_requests_block_client_role(self):
        # bug 1 hole: list/approve/reject on /api/dashboard/intake-requests/
        # only checked IsAuthenticated, so a client-portal token could read
        # every prospect's contact info and even approve/reject requests
        # (approve creates a real Client row). Only "create" (the public
        # intake form) should stay open to everyone/unauthenticated too.
        from dashboard.models import IntakeRequest

        req = IntakeRequest.objects.create(
            company_name="Acme Co", contact_person="Jane Doe",
            email="jane@acme.test", phone="123", city="Lahore",
        )

        client_api = self.clients_by_role["client"]
        resp = client_api.get("/api/dashboard/intake-requests/")
        self.assertEqual(resp.status_code, 403, resp.content)

        resp = client_api.post(f"/api/dashboard/intake-requests/{req.id}/approve/")
        self.assertEqual(resp.status_code, 403, resp.content)

        resp = client_api.post(f"/api/dashboard/intake-requests/{req.id}/reject/")
        self.assertEqual(resp.status_code, 403, resp.content)

        # Staff roles still work normally -- enforcement didn't lock
        # legitimate admins/employees/managers out.
        for role in ("employee", "manager", "admin"):
            with self.subTest(role=role):
                resp = self.clients_by_role[role].get("/api/dashboard/intake-requests/")
                self.assertEqual(resp.status_code, 200, resp.content)

        # The public form itself must stay reachable with no auth at all.
        anon = APIClient()
        resp = anon.post("/api/dashboard/intake-requests/", {
            "company_name": "Anon Co", "contact_person": "Bob",
            "email": "bob@anon.test",
        })
        self.assertEqual(resp.status_code, 201, resp.content)

    def test_company_stats_endpoints_block_client_role(self):
        # New-found gap: CustomerSatisfactionView, UserGrowthView and
        # DashboardNotificationsView were only IsAuthenticated (no
        # _deny_clients call), unlike the other company-stats endpoints on
        # this same dashboard. A client-role token could read them directly.
        urls = [
            "/api/dashboard/customer-satisfaction/",
            "/api/dashboard/user-growth/",
            "/api/dashboard/notifications/",
        ]
        for url in urls:
            with self.subTest(url=url):
                resp = self.clients_by_role["client"].get(url)
                self.assertEqual(resp.status_code, 403, resp.content)

        # Staff roles keep working.
        for url in urls:
            for role in ("employee", "manager", "admin"):
                with self.subTest(url=url, role=role):
                    resp = self.clients_by_role[role].get(url)
                    self.assertEqual(resp.status_code, 200, resp.content)

    def test_salary_hidden_from_non_admin_non_self(self):
        admin = self.clients_by_role["admin"]
        manager = self.clients_by_role["manager"]
        employee_user = User.objects.get(email="employee@moduleaccess.test")

        from users.models import Profile
        Profile.objects.update_or_create(user=employee_user, defaults={"salary": 50000})

        # Manager (not admin, not this employee) must not see the number.
        resp = manager.get(f"/api/employees/{employee_user.id}/")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIsNone(resp.json().get("salary"))

        # Admin sees it.
        resp = admin.get(f"/api/employees/{employee_user.id}/")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json().get("salary"), 50000.0)

        # The employee sees their own.
        resp = self.clients_by_role["employee"].get(f"/api/employees/{employee_user.id}/")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json().get("salary"), 50000.0)

    def test_module_permission_seeds_from_defaults_not_blank(self):
        """Regression test for issue #2: saving ONE flag for a role/module
        that has no row yet must seed the other three from
        users.access.DEFAULT_MODULE_FLAGS, not leave them False."""
        admin = self.clients_by_role["admin"]

        # manager/Sales has never been saved -> defaults give it full
        # access (see DEFAULT_MODULE_FLAGS["manager"]["Sales"]).
        resp = admin.put(
            "/api/auth/module-permissions/",
            {"role": "manager", "module": "Sales", "flag": "delete", "value": False},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertEqual(body["delete"], False)
        # The three flags nobody touched must still reflect the seeded
        # defaults (all True for manager/Sales), not the model's blank
        # False fallback.
        self.assertTrue(body["view"])
        self.assertTrue(body["create"])
        self.assertTrue(body["edit"])