"""Shared vocabulary for the Reports backend.

`module` is the human label the Reports page groups activity by (it lines up
with the sidebar pages). `category` maps a module onto the six report
categories ReportsPage.jsx already has (Financial / Project / Employee /
Client / Sales / Task), so activity can be filtered the same way reports are.
"""

MODULES = [
    "Auth", "Users", "Employees", "Projects", "Tasks", "Clients", "Sales",
    "Income", "Expenses", "Meetings", "Messages", "Settings", "Reports",
    "Dashboard", "General",
]

MODULE_TO_CATEGORY = {
    "Expenses": "Financial",
    "Income": "Financial",
    "Projects": "Project",
    "Employees": "Employee",
    "Users": "Employee",
    "Meetings": "Employee",
    "Clients": "Client",
    "Sales": "Sales",
    "Tasks": "Task",
}

# First URL segment after /api/ -> module. Used only by the request-level
# fallback logger (middleware) for actions that don't touch a tracked model.
PATH_TO_MODULE = {
    "auth": "Users",
    "settings": "Settings",
    "dashboard": "Dashboard",
    "projects": "Projects",
    "tasks": "Tasks",
    "messages": "Messages",
    "sales": "Sales",
    "expenses": "Expenses",
    "meetings": "Meetings",
    "employees": "Employees",
    "reports": "Reports",
}

# Actions a browser is allowed to report through POST /activity/track/.
TRACKABLE_ACTIONS = ("view", "download", "export", "click", "search")

# Field names whose VALUES must never be copied into the log. The fact that
# they changed is still recorded (as "•••") — useful for security review —
# but the old/new value is not.
SENSITIVE_FIELDS = {
    "password", "account_number", "iban", "cnic", "salary", "branch_code",
}
SENSITIVE_FIELD_HINTS = ("token", "secret", "otp", "api_key", "apikey", "private_key")
REDACTED = "•••"

# Never worth a diff line (bookkeeping timestamps / presence pings).
IGNORED_FIELDS = {"updated_at", "modified_at", "last_active_at", "last_login", "seen_by"}

# Requests the fallback logger should never record (noisy or already covered).
DEFAULT_IGNORED_PATHS = (
    r"^/api/messages/thread/\d+/read/",
    r"^/api/messages/calls/",
    r"^/api/messages/push/",
    r"^/api/messages/conversations/",
    r"^/api/auth/(me|ws-ticket|verify-password)/",
    r"^/api/reports/activity/track/",
)

CATEGORIES = ["Financial", "Project", "Employee", "Client", "Sales", "Task"]

MAX_DAILY_FILES = 10
MAX_DAILY_FILE_MB = 100
