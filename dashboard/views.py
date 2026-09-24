import secrets
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings as dj_settings
from django.core.mail import send_mail
from django.utils import timezone
from django.contrib.auth import get_user_model
from django.db.models import Sum, Q, DecimalField, Count
from django.db.models.functions import Coalesce
from rest_framework import viewsets, permissions, status
from rest_framework.authtoken.models import Token
from rest_framework.exceptions import PermissionDenied
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.decorators import action

from .models import (
    Client, Order, Expense, Income, Invoice, ModuleRequest, IntakeRequest,
    ActivityLogEntry, ClientMessage, Document,
)
# The real, actually-used Expense model backing ExpensesPage.jsx lives in
# the separate `expenses` app (category/project/payment/status/receipt
# fields) — NOT this app's own `Expense` above, which nothing in the
# frontend ever writes to. Aliased to avoid clashing with the `Expense`
# import above; only used for the dashboard "Total Purchase" figure and
# the notifications feed below, which need the numbers actually there.
from expenses.models import Expense as RealExpense, ExpenseStatus
# SalesPage.jsx's real sales ledger (client/project/amount/date/status)
# lives in the separate `sales` app's Sale model — used below so the
# dashboard's "Total Sales" card matches what SalesPage.jsx itself shows,
# instead of Income (which is a different thing: money actually received,
# not sales/deals logged — those two numbers will legitimately differ
# whenever a sale hasn't been paid yet).
from sales.models import Sale
from users.access import can_perform, role_category
from users.throttles import PortalLoginRateThrottle, PortalEmailKeyedThrottle
from reports.services import log_activity as audit_log  # Reports-page audit trail (never raises)
from employees.permissions import IsStaff
from .serializers import (
    ClientSerializer,
    OrderSerializer,
    ExpenseSerializer,
    IncomeSerializer,
    InvoiceSerializer,
    ModuleRequestSerializer,
    IntakeRequestSerializer,
    ActivityLogEntrySerializer,
    ClientMessageSerializer,
    DocumentSerializer,
)


def log_activity(client, text):
    ActivityLogEntry.objects.create(client=client, text=text)


class ClientPortalReadOnly(permissions.BasePermission):
    # A logged-in client (role="client") can only ever GET/HEAD/OPTIONS
    # through the generic CRUD routes on these viewsets -- never create,
    # update, or delete a Client/Order/Expense/Income/Invoice row
    # directly, even their own. The few actions a client genuinely needs
    # to POST to (submit-payment, module-request create, messages,
    # support, profile-pic) explicitly override this per-action -- see
    # each viewset below. Staff (any other role) are unaffected.

    def has_permission(self, request, view):
        if getattr(request.user, "role", None) == "client":
            return request.method in permissions.SAFE_METHODS
        return True

# Same checklist-per-project-type ClientsPage.jsx's PROJECT_TYPES ships —
# kept in sync by hand since the frontend owns the source of truth here.
# Used only by IntakeRequestViewSet.approve to build a new project's
# modules the same way the Add Client form does.
PROJECT_TYPE_MODULES = {
    "Website": ["UI/UX Design", "Frontend", "Backend", "Database", "Deployment"],
    "Mobile App": ["UI/UX Design", "Frontend", "Backend", "API Integration", "Testing", "Deployment"],
    "E-commerce": ["UI/UX Design", "Frontend", "Backend", "Payment Integration", "Testing", "Deployment"],
    "Custom Software": ["Requirement Analysis", "Backend", "Frontend", "Database", "Testing", "Deployment"],
    "Graphic Design": ["Concept & Moodboard", "Design Drafts", "Client Revisions", "Final Artwork"],
    "Other": ["Planning", "Development", "Testing", "Deployment"],
}

# A module whose name is in here needs a real link (deployed URL, repo,
# staging link, ...) before it's marked done — mirrors ClientsPage.jsx's
# LINK_REQUIRED_MODULES. tasks.Task.requires_link is set from this.
LINK_REQUIRED_MODULES = {"Frontend", "Backend", "Deployment"}

User = get_user_model()


def get_range(period):
    """Current period ka (start, end) aur uske pichlay equal-length period
    ka (prev_start, prev_end) return karta hai — isi se % delta nikalta hai."""
    now = timezone.now()
    if period == "week":
        start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "quarter":
        q_start_month = ((now.month - 1) // 3) * 3 + 1
        start = now.replace(month=q_start_month, day=1, hour=0, minute=0, second=0, microsecond=0)
    elif period == "year":
        start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    else:  # "month" (default)
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    end = now
    length = end - start
    prev_end = start
    prev_start = start - length
    return start, end, prev_start, prev_end


def pct_delta(current, previous):
    if not previous:
        return "+ 0%"
    change = ((current - previous) / previous) * 100
    sign = "+" if change >= 0 else "-"
    return f"{sign} {abs(round(change))}%"


def _deny_clients(user):
    """Company-wide figures (sales / income / spend / profit) are staff-only.
    A token that belongs to a client-role account (Client Portal login) is
    refused outright -- the Client Portal never calls these endpoints."""
    if role_category(getattr(user, "role", None)) == "client":
        raise PermissionDenied("Company statistics are not available for client accounts.")


def _can_see_money(user):
    """True if this user is allowed to view the Sales or Income pages (page
    access + module "view" flag, same rule the React app applies)."""
    return can_perform(user, "view", "Sales") or can_perform(user, "view", "Income")


class DashboardStatsView(APIView):
    """GET /api/dashboard/stats/?period=week|month|quarter|year
    Frontend STAT_CARDS order se match karta hai: Total Sales, Total
    Purchase, Total Profit, New Customers."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        _deny_clients(request.user)
        period = request.query_params.get("period", "month")
        start, end, prev_start, prev_end = get_range(period)

        def sales_sum(a, b):
            # Sums SalesPage.jsx's own Sale entries (client/project/amount)
            # so this dashboard card matches the Sales page's own "Total
            # Sales" figure, rather than Income (a different, unrelated
            # ledger — money actually received, which can be far ahead of
            # or behind what's been logged as sold). Every Sale counts
            # regardless of Paid/Partial/Pending status, matching
            # SalesPage.jsx's own totalSales (a plain reduce over the
            # full list, with status only filtering a separate stat).
            return Sale.objects.filter(
                date__gte=a.date(), date__lte=b.date()
            ).aggregate(t=Coalesce(Sum("amount"), 0, output_field=DecimalField()))["t"]

        def purchase_sum(a, b):
            # Same fix, plus: real expenses live in the separate
            # `expenses` app (RealExpense above), not this app's own
            # unused Expense model.
            return RealExpense.objects.filter(
                status=ExpenseStatus.APPROVED, date__gte=a.date(), date__lte=b.date()
            ).aggregate(t=Coalesce(Sum("amount"), 0, output_field=DecimalField()))["t"]

        def customers_count(a, b):
            return Client.objects.filter(created_at__gte=a, created_at__lt=b).count()

        sales, prev_sales = sales_sum(start, end), sales_sum(prev_start, prev_end)
        purchase, prev_purchase = purchase_sum(start, end), purchase_sum(prev_start, prev_end)
        profit, prev_profit = sales - purchase, prev_sales - prev_purchase
        customers, prev_customers = customers_count(start, end), customers_count(prev_start, prev_end)

        data = [
            {"label": "Total Sales", "value": f"PKR {sales:,.0f}", "delta": pct_delta(sales, prev_sales)},
            {"label": "Total Purchase", "value": f"PKR {purchase:,.0f}", "delta": pct_delta(purchase, prev_purchase)},
            {"label": "Total Profit", "value": f"PKR {profit:,.0f}", "delta": pct_delta(profit, prev_profit)},
            {"label": "New Customers", "value": str(customers), "delta": pct_delta(customers, prev_customers)},
        ]

        # Same card -> page mapping the Dashboard cards use (STAT_CARDS in
        # Dashboard.jsx: Sales / Expenses / Income): a user only gets the
        # real money figures for pages they may open. Same response shape
        # either way, so the Dashboard keeps rendering; hidden cards show a
        # dash instead of the number.
        see_sales = can_perform(request.user, "view", "Sales")
        see_expenses = can_perform(request.user, "view", "Expenses")
        hidden_labels = set()
        if not see_sales:
            hidden_labels.add("Total Sales")
        if not see_expenses:
            hidden_labels.add("Total Purchase")
        if not (see_sales and see_expenses):
            hidden_labels.add("Total Profit")  # profit = sales - purchase
        for row in data:
            if row["label"] in hidden_labels:
                row["value"] = "—"
                row["delta"] = "—"
        return Response(data)


class MostOrdersByCountryView(APIView):
    """GET /api/dashboard/most-orders-by-country/?limit=3
    Which countries the most clients are actually FROM — ranked by how
    many clients share that country (ties broken by that country's
    combined approved-order spend), each row showing the COUNTRY's own
    name/flag plus its top client's photo for a face on the card.

    FIX: this used to rank individual CLIENTS by spend, one row per
    client — so with several clients from different countries, some
    countries (and their flags) never showed up at all, and a country
    with several clients got no credit for that; "most clients are from
    Pakistan" wasn't something this could ever say. Grouping by country
    first is what actually answers "which country do our clients mostly
    come from", ties included — if two countries are tied on client
    count, both appear (as long as `limit` covers them).
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        _deny_clients(request.user)
        see_money = _can_see_money(request.user)
        limit = int(request.query_params.get("limit", 3))
        country_rows = (
            Client.objects.exclude(country_code="")
            .values("country_code", "country")
            .annotate(
                client_count=Count("id", distinct=True),
                total_spent=Coalesce(
                    Sum("orders__amount", filter=Q(orders__status="approved")),
                    0,
                    output_field=DecimalField(),
                ),
            )
            .order_by("-client_count", "-total_spent")[:limit]
        )

        data = []
        for i, row in enumerate(country_rows):
            # Best "face" for the card: whichever client from this
            # country has spent the most (falls back to most recently
            # added when nobody from there has any approved orders yet).
            top_client = (
                Client.objects.filter(country_code=row["country_code"])
                .annotate(
                    spent=Coalesce(
                        Sum("orders__amount", filter=Q(orders__status="approved")),
                        0,
                        output_field=DecimalField(),
                    )
                )
                .order_by("-spent", "-created_at")
                .first()
            )
            avatar = ""
            if top_client:
                avatar = (
                    request.build_absolute_uri(top_client.avatar.url)
                    if top_client.avatar
                    else top_client.avatar_url
                )
            count = row["client_count"]
            data.append({
                "rank": i + 1,
                "name": row["country"] or row["country_code"].upper(),
                "text": f"{count} client{'s' if count != 1 else ''} from" if count > 1 else "client from",
                "amount": (
                    (f"PKR {row['total_spent']:,.0f}" if row["total_spent"] > 0 else "No orders yet")
                    if see_money
                    else "—"
                ),
                "city": top_client.city if top_client else "",
                "countryCode": row["country_code"],
                "avatar": avatar,
            })
        return Response(data)


class LowPurchaseClientsView(APIView):
    """GET /api/dashboard/low-purchase-clients/?limit=5
    Sabse kam total purchase karne wale clients — jo clients "kam kharcha
    kar rahe hain" unko yahan se pehchana ja sakta hai."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        _deny_clients(request.user)
        see_money = _can_see_money(request.user)
        limit = int(request.query_params.get("limit", 5))
        clients = Client.objects.annotate(
            spent=Coalesce(
                Sum("orders__amount", filter=Q(orders__status="approved")),
                0,
                output_field=DecimalField(),
            )
        ).order_by("spent")[:limit]
        data = [
            {
                "id": c.id,
                "name": c.name,
                "country": c.country,
                "countryCode": c.country_code,
                "totalSpent": f"PKR {c.spent:,.0f}" if see_money else "—",
                "ordersCount": c.orders.count(),
            }
            for c in clients
        ]
        return Response(data)


class CustomerSatisfactionView(APIView):
    """GET /api/dashboard/customer-satisfaction/?period=weekly|monthly|yearly
    Real "satisfaction" proxy = order approval rate (approved orders ÷
    total orders placed) for each bucket. Feeds the Dashboard's
    Statistics card (Customer Satisfaction bars)."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        _deny_clients(request.user)
        period = request.query_params.get("period", "weekly").lower()
        now = timezone.now()

        if period == "yearly":
            orders = Order.objects.filter(created_at__year=now.year).values("created_at", "status")
            labels = ["Q1", "Q2", "Q3", "Q4"]
            n_buckets = 4

            def bucket_of(created_at):
                return (created_at.month - 1) // 3

        elif period == "monthly":
            orders = Order.objects.filter(
                created_at__year=now.year, created_at__month=now.month
            ).values("created_at", "status")
            labels = ["Week 1", "Week 2", "Week 3", "Week 4"]
            n_buckets = 4

            def bucket_of(created_at):
                return min((created_at.day - 1) // 7, 3)

        else:  # weekly
            orders = Order.objects.filter(created_at__gte=now - timedelta(days=90)).values(
                "created_at", "status"
            )
            labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
            n_buckets = 7

            def bucket_of(created_at):
                return created_at.weekday()

        buckets = {i: {"total": 0, "approved": 0} for i in range(n_buckets)}
        for o in orders:
            idx = bucket_of(o["created_at"])
            buckets[idx]["total"] += 1
            if o["status"] == "approved":
                buckets[idx]["approved"] += 1

        bars = []
        for i, label in enumerate(labels):
            b = buckets[i]
            value = round((b["approved"] / b["total"]) * 100) if b["total"] else 0
            bars.append({"day": label, "value": value, "delta": f"+{value}%"})

        total_all = sum(b["total"] for b in buckets.values())
        approved_all = sum(b["approved"] for b in buckets.values())
        overall = round((approved_all / total_all) * 100) if total_all else 0

        return Response(
            {
                "headline": f"+{overall}%",
                "note": "Customer satisfaction increases every week"
                if period == "weekly"
                else f"Order approval rate — {period}",
                "bars": bars,
            }
        )


class SalesOverviewView(APIView):
    """GET /api/dashboard/sales-overview/?period=week|month|quarter|year
    Real received-Income totals per day (week/month) or per month
    (quarter/year), plus a "base" running-average trend line — feeds
    the Sales Overview line chart.

    FIX: same root cause as DashboardStatsView above — sourced from the
    Order model, which nothing in the app ever actually creates rows
    in, so this chart was always a flat line at zero. Now sourced from
    real, received Income entries instead.
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        _deny_clients(request.user)
        period = request.query_params.get("period", "month")
        start, end, _, _ = get_range(period)

        incomes = Income.objects.filter(
            status="Received", date__gte=start, date__lte=end
        ).values("date", "amount")
        if not _can_see_money(request.user):
            # No Sales/Income access: same chart shape, but no real amounts.
            incomes = incomes.none()

        buckets = {}
        if period in ("quarter", "year"):
            cur = start.replace(day=1)
            while cur <= end:
                key = f"{cur.year}-{cur.month:02d}"
                buckets[key] = {"label": cur.strftime("%b"), "total": 0.0}
                cur = (cur.replace(year=cur.year + 1, month=1)
                       if cur.month == 12 else cur.replace(month=cur.month + 1))
            for inc in incomes:
                d = inc["date"]
                key = f"{d.year}-{d.month:02d}"
                if key in buckets:
                    buckets[key]["total"] += float(inc["amount"])
        else:
            cur = start.date()
            end_date = end.date()
            while cur <= end_date:
                key = cur.isoformat()
                buckets[key] = {"label": f"{cur.strftime('%b')} {cur.day}", "total": 0.0}
                cur += timedelta(days=1)
            for inc in incomes:
                key = inc["date"].isoformat()
                if key in buckets:
                    buckets[key]["total"] += float(inc["amount"])

        running_sum = 0.0
        data = []
        for i, key in enumerate(sorted(buckets.keys()), start=1):
            b = buckets[key]
            running_sum += b["total"]
            data.append({"day": b["label"], "sales": round(b["total"]), "base": round(running_sum / i)})

        return Response(data)


class UserGrowthView(APIView):
    """GET /api/dashboard/user-growth/?period=2h|32h|week|month
    Real registered-users data (from AUTH_USER_MODEL) for the "User
    Growth" widget — "total" = new users joined within the window,
    "delta" = % change vs the previous equal window, "highlightNote" =
    how many joined today."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        _deny_clients(request.user)
        period = request.query_params.get("period", "month").lower()
        now = timezone.now()

        windows = {
            "2h": timedelta(hours=2),
            "32h": timedelta(hours=32),
            "week": timedelta(days=7),
            "month": timedelta(days=30),
        }
        window = windows.get(period, windows["month"])

        current_count = User.objects.filter(
            status="approved", date_joined__gte=now - window, date_joined__lte=now
        ).count()
        prev_count = User.objects.filter(
            status="approved", date_joined__gte=now - (2 * window), date_joined__lt=now - window
        ).count()

        total_users = User.objects.filter(status="approved").count()
        today_count = User.objects.filter(status="approved", date_joined__date=now.date()).count()
        progress = min(100, round((current_count / total_users) * 100)) if total_users else 0

        notes = {
            "2h": "Checking last 2 hours",
            "32h": "Checking last 32 hours",
            "week": "Checking this week",
            "month": "Checking totally",
        }

        return Response(
            {
                "total": f"{current_count:,}",
                "delta": pct_delta(current_count, prev_count),
                "progress": progress,
                "note": notes.get(period, "Checking totally"),
                "highlightNote": f"+{today_count} today",
            }
        )


class DashboardNotificationsView(APIView):
    """GET /api/dashboard/notifications/?limit=6
    Real recent activity across the app, for the bell icon dropdown.

    FIX: the bell used to show a hardcoded NOTIFICATIONS constant in
    Dashboard.jsx — always the same 3 fake items ("Jenny placed an
    order worth $120", ...) no matter what actually happened. This
    pulls together the handful of things across the app that are
    genuinely worth a notification — new clients, real received
    payments, and anything actually awaiting someone's review/decision
    (pending expenses, module start requests, new project intake
    requests) — sorted newest-first.
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        _deny_clients(request.user)
        limit = int(request.query_params.get("limit", 6))
        items = []

        for c in Client.objects.order_by("-created_at")[:limit]:
            items.append({
                "title": "New client added",
                "desc": f"{c.name} was added as a new client",
                "time": c.created_at,
            })

        for inc in Income.objects.filter(status="Received").order_by("-created_at")[:limit]:
            items.append({
                "title": "Payment received",
                "desc": f"{inc.description or inc.project or 'A payment'} — PKR {inc.amount:,.0f}",
                "time": inc.created_at,
            })

        for exp in RealExpense.objects.filter(status=ExpenseStatus.PENDING).order_by("-created_at")[:limit]:
            items.append({
                "title": "Expense awaiting approval",
                "desc": f"{exp.title} — PKR {exp.amount:,.0f}",
                "time": exp.created_at,
            })

        for mr in ModuleRequest.objects.filter(status="pending").select_related("client", "module").order_by("-requested_at")[:limit]:
            items.append({
                "title": "Module request pending",
                "desc": f"{mr.client.name} wants to start \"{mr.module.name}\"",
                "time": mr.requested_at,
            })

        for ir in IntakeRequest.objects.filter(status="pending").order_by("-created_at")[:limit]:
            items.append({
                "title": "New project request",
                "desc": f"{ir.company_name or ir.contact_person} submitted a new project request",
                "time": ir.created_at,
            })

        items.sort(key=lambda x: x["time"], reverse=True)
        data = [
            {"id": i + 1, "title": it["title"], "desc": it["desc"], "time": it["time"].isoformat()}
            for i, it in enumerate(items[:limit])
        ]
        return Response(data)


class ClientViewSet(viewsets.ModelViewSet):
    """Full CRUD — ClientsPage.jsx ka asli backend (list/create/update/
    delete), real Postgres rows, nothing in localStorage anymore.

    Query params (all optional):
      ?status=active|inactive
      ?industry=<exact industry>
      ?manager=<user id>
      ?search=<matches name, contact person, email, phone>
    """

    queryset = (
        Client.objects.select_related("manager", "portal_user")
        .prefetch_related(
            "projects",
            "projects__modules",
            # FIX (Client Portal attachments always empty): the module
            # list was prefetched, but not each module's own files or
            # its subtasks (+ THEIR files) — ClientProjectModuleSerializer
            # now reads obj.files / obj.subtasks / subtask.files (see
            # dashboard/serializers.py's _module_attachments), so without
            # these every one of those reads would fire its own extra
            # query per module instead of using this single prefetch.
            "projects__modules__files",
            "projects__modules__subtasks",
            "projects__modules__subtasks__files",
            "documents",
            "documents__uploaded_by",
        )
        .all()
    )
    serializer_class = ClientSerializer
    permission_classes = [permissions.IsAuthenticated, ClientPortalReadOnly]

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user

        # A logged-in client only ever sees their own single row here --
        # everyone else (staff) keeps the full admin view + filters below.
        if getattr(user, "role", None) == "client":
            client_profile = getattr(user, "client_profile", None)
            return qs.filter(pk=client_profile.pk) if client_profile else qs.none()

        p = self.request.query_params

        status_ = p.get("status")
        if status_:
            qs = qs.filter(status=status_)

        industry = p.get("industry")
        if industry:
            qs = qs.filter(industry=industry)

        manager = p.get("manager")
        if manager:
            qs = qs.filter(manager_id=manager)

        search = p.get("search")
        if search:
            qs = qs.filter(
                Q(name__icontains=search)
                | Q(contact_person__icontains=search)
                | Q(email__icontains=search)
                | Q(phone__icontains=search)
            )

        return qs

    def perform_create(self, serializer):
        user = self.request.user
        serializer.save(created_by=user if user and user.is_authenticated else None)

    @action(detail=True, methods=["post"], url_path="portal-access")
    def portal_access(self, request, pk=None):
        """Generate (or reset) this client's REAL ClientPortal.jsx login —
        a genuine users.User row with role="client". Returns the plain
        password exactly once, same as the frontend's KeyRound/Copy UI
        already expects; it is never stored or returned again after this."""
        client = self.get_object()
        clean_email = (client.email or "").strip().lower()
        if not clean_email:
            return Response(
                {"detail": "Client needs an email on file before portal access can be generated."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        password = secrets.token_urlsafe(9)  # ~12 char random password, URL-safe to copy/share
        if client.portal_user:
            portal_user = client.portal_user
            portal_user.set_password(password)
            portal_user.email = clean_email
            portal_user.name = client.contact_person or client.name
            portal_user.role = "client"
            portal_user.status = "approved"
            portal_user.save()
        else:
            existing_user = User.objects.filter(email__iexact=clean_email).first()
            if existing_user:
                if existing_user.role != "client":
                    return Response(
                        {"detail": f"An employee or staff account already exists with email '{clean_email}'."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                other_client = getattr(existing_user, "client_profile", None)
                if other_client and other_client.pk != client.pk:
                    return Response(
                        {"detail": f"Portal access with email '{clean_email}' is already linked to another client ('{other_client.name}')."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                portal_user = existing_user
                portal_user.set_password(password)
                portal_user.name = client.contact_person or client.name
                portal_user.role = "client"
                portal_user.status = "approved"
                portal_user.save()
            else:
                portal_user = User.objects.create_user(
                    email=clean_email,
                    password=password,
                    name=client.contact_person or client.name,
                    role="client",
                    status="approved",
                )
            client.portal_user = portal_user
            client.save(update_fields=["portal_user"])

        return Response({"username": clean_email, "password": password})

    @action(detail=True, methods=["post"], url_path="revoke-portal-access")
    def revoke_portal_access(self, request, pk=None):
        client = self.get_object()
        if client.portal_user:
            client.portal_user.status = "deactivated"
            client.portal_user.save(update_fields=["status"])
        return Response(ClientSerializer(client).data)

    @action(
        detail=False, methods=["post"], url_path="portal-login",
        permission_classes=[permissions.AllowAny],
        throttle_classes=[PortalLoginRateThrottle, PortalEmailKeyedThrottle],
    )
    def portal_login(self, request):
        """POST /api/dashboard/clients/portal-login/  { id, email, password }

        Client Portal sign-in. The client must present ALL THREE of: their
        Client ID, the email on file, and the portal password an admin
        generated for them ("Generate Portal Access" / "Reset Portal
        Password" on the Clients page -> portal-access above). Client ID is
        just the row's sequential pk, so on its own it (and the email) prove
        nothing -- the password is the actual secret.

        Hardening, all mirrored from the staff LoginView:
          * throttled per IP (5/min) AND per email (10/min)
          * persistent per-account lockout via User.failed_login_attempts /
            locked_until (survives cache restarts)
          * ONE generic 401 for "no such client", "wrong email", "no portal
            access yet" and "wrong password", so this endpoint can't be used
            to discover which Client IDs / emails exist
          * a dummy hash on the unknown-account path, so response time
            doesn't reveal it either
        Returns the same {token, client} payload as before on success."""
        client_id = str(request.data.get("id", "")).strip()
        email = str(request.data.get("email", "")).strip().lower()
        password = str(request.data.get("password", ""))
        if not client_id or not email or not password:
            return Response(
                {"detail": "Client ID, email and password are all required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        def _invalid():
            return Response(
                {"detail": "Invalid Client ID, email or password. If you haven't received your portal password yet, please contact your account manager."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        client = None
        if client_id.isdigit() and len(client_id) <= 18:
            client = (
                Client.objects.select_related("portal_user")
                .filter(pk=int(client_id), email__iexact=email)
                .first()
            )
        portal_user = client.portal_user if client else None

        if portal_user is None:
            # Unknown ID/email pair, or portal access never generated. Burn
            # one password hash so this path takes about as long as a real
            # wrong-password attempt, then give the same generic answer.
            User().set_password(password)
            audit_log(
                action="portal_login_failed", module="Auth",
                description=f"Failed Client Portal login for {email} (Client ID {client_id})",
                actor_email=email, metadata={"email": email, "client_id": client_id}, dedupe_seconds=30,
            )
            return _invalid()

        if portal_user.is_locked():
            audit_log(
                action="portal_login_blocked", module="Auth",
                description=f"Client Portal login blocked (account temporarily locked) for {email}",
                actor_email=email, metadata={"email": email, "client_id": client_id}, dedupe_seconds=30,
            )
            return Response(
                {"detail": "Too many failed attempts. Please try again in a few minutes."},
                status=status.HTTP_423_LOCKED,
            )

        if not portal_user.check_password(password):
            portal_user.register_failed_login()
            audit_log(
                action="portal_login_failed", module="Auth",
                description=f"Failed Client Portal login for {email} (Client ID {client_id})",
                actor_email=email, metadata={"email": email, "client_id": client_id}, dedupe_seconds=30,
            )
            return _invalid()

        # Password is correct from here on, so it's safe to be specific.
        if portal_user.status != "approved" or not portal_user.is_active:
            return Response(
                {"detail": "Portal access for this account has been deactivated. Please contact your account manager."},
                status=status.HTTP_403_FORBIDDEN,
            )

        portal_user.reset_failed_login()
        token, _ = Token.objects.get_or_create(user=portal_user)
        audit_log(action="login", user=portal_user, module="Auth", description="Client Portal login")
        return Response({"token": token.key, "client": ClientSerializer(client, context={"request": request}).data})

    @action(
        detail=True, methods=["patch", "delete"], url_path="profile-pic",
        permission_classes=[permissions.IsAuthenticated],
    )
    def profile_pic(self, request, pk=None):
        """A client's own Profile Settings photo upload/removal — reuses
        the same `avatar` field ClientsPage.jsx's Add/Edit Client form
        writes to, so staff and the client are editing the same field.
        get_object() already scopes to the caller's own row for the
        client role (see get_queryset above), so a client can never hit
        another client's pk here."""
        client = self.get_object()
        if request.method == "DELETE":
            client.avatar = None
        else:
            client.avatar = request.FILES.get("photo") or None
        client.save(update_fields=["avatar"])
        return Response(ClientSerializer(client, context={"request": request}).data)


class OrderViewSet(viewsets.ModelViewSet):
    queryset = Order.objects.select_related("client").all()
    serializer_class = OrderSerializer
    permission_classes = [permissions.IsAuthenticated, ClientPortalReadOnly]

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user
        if getattr(user, "role", None) == "client":
            client_profile = getattr(user, "client_profile", None)
            return qs.filter(client=client_profile) if client_profile else qs.none()
        return qs


class ExpenseViewSet(viewsets.ModelViewSet):
    # Internal bookkeeping only -- a client should never see or touch
    # this, regardless of which client it is, so no scoping needed:
    # just an empty queryset for the client role.
    queryset = Expense.objects.all()
    serializer_class = ExpenseSerializer
    permission_classes = [permissions.IsAuthenticated, ClientPortalReadOnly]

    def get_queryset(self):
        qs = super().get_queryset()
        if getattr(self.request.user, "role", None) == "client":
            return qs.none()
        return qs


class IncomeViewSet(viewsets.ModelViewSet):
    """/api/dashboard/incomes/ — IncomePage.jsx ka asli CRUD backend.

    Query params (sab optional — jitne dein utni filtering DB level par
    lagti hai, front-end jaisa hi client-side filter dobara likhne ki
    zaroorat nahi):
      ?status=Received|Pending|Overdue
      ?project=<exact project name>
      ?method=Bank Transfer|JazzCash|Easypaisa|Cash in Hand
      ?search=<matches description, case-insensitive>
      ?date_from=YYYY-MM-DD&date_to=YYYY-MM-DD
      ?amount_min=<number>&amount_max=<number>
    """

    serializer_class = IncomeSerializer
    permission_classes = [permissions.IsAuthenticated, ClientPortalReadOnly]

    def get_queryset(self):
        # Internal ledger only -- never scoped/shown to the client role.
        if getattr(self.request.user, "role", None) == "client":
            return Income.objects.none()

        qs = Income.objects.select_related("created_by").all()
        p = self.request.query_params

        status_ = p.get("status")
        if status_ and status_ not in ("All Incomes", "all"):
            qs = qs.filter(status=status_)

        project = p.get("project")
        if project and project not in ("All Projects", "all"):
            qs = qs.filter(project=project)

        method = p.get("method")
        if method and method not in ("All Payment Methods", "all"):
            qs = qs.filter(method=method)

        search = p.get("search")
        if search:
            qs = qs.filter(description__icontains=search)

        date_from = p.get("date_from")
        if date_from:
            qs = qs.filter(date__gte=date_from)
        date_to = p.get("date_to")
        if date_to:
            qs = qs.filter(date__lte=date_to)

        amount_min = p.get("amount_min")
        if amount_min:
            qs = qs.filter(amount__gte=amount_min)
        amount_max = p.get("amount_max")
        if amount_max:
            qs = qs.filter(amount__lte=amount_max)

        return qs

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)


class InvoiceViewSet(viewsets.ModelViewSet):
    """/api/dashboard/invoices/ — ClientsPage.jsx's Billing tab: one row
    per milestone. ?client=<id> to scope to one client's invoices."""

    queryset = Invoice.objects.select_related("client", "project").all()
    serializer_class = InvoiceSerializer
    permission_classes = [permissions.IsAuthenticated, ClientPortalReadOnly]

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user
        if getattr(user, "role", None) == "client":
            client_profile = getattr(user, "client_profile", None)
            return qs.filter(client=client_profile) if client_profile else qs.none()
        client_id = self.request.query_params.get("client")
        if client_id:
            qs = qs.filter(client_id=client_id)
        return qs

    @action(
        detail=True, methods=["post"], url_path="submit-payment",
        permission_classes=[permissions.IsAuthenticated],  # overrides ClientPortalReadOnly -- this IS a client action
    )
    def submit_payment(self, request, pk=None):
        """Client Portal side: attach a payment-proof screenshot, move
        Pending -> Submitted, awaiting admin confirmation below.
        get_object() already scopes to the caller's own invoices for the
        client role, so a client can never hit someone else's pk here."""
        invoice = self.get_object()
        if invoice.status == "paid":
            return Response({"detail": "This invoice is already paid."}, status=status.HTTP_400_BAD_REQUEST)
        proof = request.FILES.get("payment_proof")
        if not proof:
            return Response({"detail": "payment_proof file is required."}, status=status.HTTP_400_BAD_REQUEST)
        invoice.payment_proof = proof
        invoice.status = "submitted"
        invoice.submitted_at = timezone.now()
        invoice.save()
        log_activity(invoice.client, f"Payment screenshot submitted for Milestone {invoice.milestone_number}/{invoice.milestone_total} — awaiting confirmation")
        return Response(InvoiceSerializer(invoice).data)

    @action(detail=True, methods=["post"], url_path="confirm")
    def confirm(self, request, pk=None):
        """Admin side ONLY (inherits the viewset's [IsAuthenticated,
        ClientPortalReadOnly], which blocks POST for the client role) --
        confirm a submitted payment as actually received. This is what
        moves the amount from `outstanding` into `total_spent` on the
        client (dashboard.Order-style bookkeeping, via
        ClientSerializer.get_outstanding)."""
        invoice = self.get_object()
        already_paid = invoice.paid_amount or 0
        remaining = invoice.amount - already_paid
        invoice.status = "paid"
        invoice.paid_amount = invoice.amount
        invoice.paid_at = timezone.now()
        invoice.save()
        if remaining > 0:
            # Only book an Order for whatever hadn't already been recorded
            # via record-payment below, so a milestone that was partially
            # paid first and then confirmed doesn't get double-counted in
            # the client's outstanding/total_spent figures.
            Order.objects.create(
                client=invoice.client,
                amount=remaining,
                status="approved",
                note=f"Milestone {invoice.milestone_number}/{invoice.milestone_total}"
                + (f" — {invoice.project.name}" if invoice.project_id else ""),
            )
        log_activity(invoice.client, f"Payment confirmed for Milestone {invoice.milestone_number}/{invoice.milestone_total} — thank you!")
        return Response(InvoiceSerializer(invoice).data)

    @action(detail=True, methods=["post"], url_path="record-payment")
    def record_payment(self, request, pk=None):
        """Admin side ONLY — ClientsPage.jsx's "Record Payment" modal,
        used for payments the admin logs manually (bank transfer, cash,
        ...) rather than a client-submitted screenshot. Unlike confirm()
        above this supports PARTIAL amounts: the invoice sits as
        "Partial" until enough payments add up to cover it, then flips
        to "Paid" automatically — same math ClientsPage.jsx used to only
        do locally, now a real Postgres row the Client Portal sees too."""
        invoice = self.get_object()
        if invoice.status == "paid":
            return Response({"detail": "This invoice is already paid."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            amt = Decimal(str(request.data.get("amount", "")))
        except (InvalidOperation, TypeError):
            amt = None
        if amt is None or amt <= 0:
            return Response({"detail": "A valid amount is required."}, status=status.HTTP_400_BAD_REQUEST)

        invoice.paid_amount = min(invoice.amount, (invoice.paid_amount or 0) + amt)
        fully_paid = invoice.paid_amount >= invoice.amount
        invoice.status = "paid" if fully_paid else "partial"
        if fully_paid:
            invoice.paid_at = timezone.now()
        invoice.save()

        Order.objects.create(
            client=invoice.client,
            amount=amt,
            status="approved",
            note=f"Payment against {invoice.number or f'Milestone {invoice.milestone_number}/{invoice.milestone_total}'}",
        )
        log_activity(invoice.client, f"PKR {amt:,.0f} payment received against {invoice.number or ('Milestone ' + str(invoice.milestone_number))}")
        return Response(InvoiceSerializer(invoice).data)


class ModuleRequestViewSet(viewsets.ModelViewSet):
    """/api/dashboard/module-requests/ — a client's "Request to start"
    click on a locked module (ClientPortal.jsx), and staff's Accept/
    Reject on it (ClientsPage.jsx). ?client=<id> / ?status=pending."""

    queryset = ModuleRequest.objects.select_related("client", "module", "module__project", "project").all()
    serializer_class = ModuleRequestSerializer
    permission_classes = [permissions.IsAuthenticated, ClientPortalReadOnly]
    http_method_names = ["get", "post", "head", "options"]  # no direct edit/delete — only accept/reject below

    def get_permissions(self):
        # "create" is the one write a client genuinely needs (asking to
        # start a locked module, or asking for a brand-new one) --
        # everything else (list/retrieve are already GET-safe;
        # accept/reject are staff-only) keeps the viewset-level
        # ClientPortalReadOnly, which blocks a client's POST.
        if self.action == "create":
            return [permissions.IsAuthenticated()]
        return super().get_permissions()

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user
        p = self.request.query_params
        if getattr(user, "role", None) == "client":
            client_profile = getattr(user, "client_profile", None)
            qs = qs.filter(client=client_profile) if client_profile else qs.none()
        else:
            client_id = p.get("client")
            if client_id:
                qs = qs.filter(client_id=client_id)
        status_ = p.get("status")
        if status_:
            qs = qs.filter(status=status_)
        return qs

    def perform_create(self, serializer):
        user = self.request.user
        if getattr(user, "role", None) == "client":
            client_profile = getattr(user, "client_profile", None)
            if not client_profile or serializer.validated_data.get("client") != client_profile:
                raise PermissionDenied("You can only request modules for your own account.")
            module = serializer.validated_data.get("module")
            project = serializer.validated_data.get("project")
            if module and module.project.client_id != client_profile.id:
                raise PermissionDenied("You can only request modules from your own projects.")
            if project and project.client_id != client_profile.id:
                raise PermissionDenied("You can only request modules for your own projects.")
        serializer.save()

    @action(detail=True, methods=["post"])
    def accept(self, request, pk=None):
        req = self.get_object()
        if req.status != "pending":
            return Response({"detail": "This request was already decided."}, status=status.HTTP_400_BAD_REQUEST)

        # Local imports to avoid a circular import (projects/tasks import
        # dashboard.Client at module load time).
        from projects.models import Module
        from tasks.models import Task

        req.status = "accepted"
        req.decided_at = timezone.now()
        req.decided_by = request.user
        req.save()

        if req.module_id:
            # Existing-module path: just unlock it.
            module = req.module
            module.unlocked = True
            module.save(update_fields=["unlocked"])
        else:
            # Custom-module path: the module doesn't exist yet — create
            # it now (unlocked from the start, since accepting IS the
            # go-ahead), then wire req.module to it.
            module = Module.objects.create(project=req.project, name=req.custom_module_name, unlocked=True)
            req.module = module
            req.save(update_fields=["module"])

        # BUG 2 FIX (auto-created module task invisible on the assigned
        # manager's TasksPage): Task.assignees is a JSON list of display
        # name strings that TasksPage.jsx filters on (GET /tasks/?assignee=
        # → assignees__contains=[name]). The task auto-created here never
        # had assignees set at all, so it matched nobody and was invisible
        # on the manager's own Task Page even though they'd been "assigned"
        # (and messaged about it). Resolve the client's manager name now so
        # it lands in the row immediately — no FK change, preserving the
        # existing name-string design tasks/models.py documents.
        manager_name = ""
        if req.client and req.client.manager_id:
            mgr = req.client.manager
            manager_name = getattr(mgr, "name", "") or getattr(mgr, "username", "") or ""

        task, _created = Task.objects.get_or_create(
            # FIX: this key must match TasksPage.jsx's own moduleTaskKey()
            # shape (client id + PROJECT NAME + module id) — it was built
            # from module.project_id (a number) instead, so the frontend's
            # client-side module-task sync never recognised this row as
            # already-linked and could create a second, duplicate task for
            # the very same module the next time it ran.
            module_task_key=f"{req.client_id}::{module.project.name}::{module.id}::",
            defaults=dict(
                title=module.name,
                project=module.project.name,
                client=req.client,
                status="Pending",
                locked=False,
                module_name=module.name,
                module_project_name=module.project.name,
                from_client_module=True,
                requires_link=module.name in LINK_REQUIRED_MODULES,
                # FIX: stamp the real Module FK so upload_zip /
                # upload_file_attachment can create ModuleFile rows and the
                # file appears in ClientPortal cross-browser.
                module=module,
                # BUG 2 FIX: stamp the manager's display name so TasksPage
                # assignee filter can find this task.
                assignees=[manager_name] if manager_name else [],
            ),
        )
        # BUG FIX (locked module/task never unlocked after an accepted
        # request): for the EXISTING-module path above, a Task row for
        # this module was already created back when the module itself was
        # first created (see ModuleViewSet._sync_module_task), stamped
        # `locked=True` because the module started out unlocked=False.
        # Task.objects.get_or_create() only ever applies `defaults` when
        # it CREATES a brand-new row — since the row already existed here,
        # `locked=False` in defaults above was silently never applied, so
        # the Task stayed locked forever even though module.unlocked had
        # just been flipped True two lines up. Force both fields on the
        # row we actually got back (new or pre-existing) so accepting a
        # request always unlocks the real Task the assignee works from.
        # Also backfill assignees if they were missing on the pre-existing row.
        needs_save = task.locked or task.module_id != module.id
        if manager_name and not task.assignees:
            task.assignees = [manager_name]
            needs_save = True
        if needs_save:
            update_fields = ["locked", "module"]
            task.locked = False
            task.module = module
            if manager_name and "assignees" not in update_fields and task.assignees:
                update_fields.append("assignees")
            task.save(update_fields=update_fields)
        log_activity(req.client, f'Your request to start "{module.name}" was accepted')
        return Response(ModuleRequestSerializer(req).data)

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        req = self.get_object()
        if req.status != "pending":
            return Response({"detail": "This request was already decided."}, status=status.HTTP_400_BAD_REQUEST)
        req.status = "rejected"
        req.decided_at = timezone.now()
        req.decided_by = request.user
        req.save()
        log_activity(req.client, f'Your request to start "{req.module_name}" was declined')
        return Response(ModuleRequestSerializer(req).data)


class IntakeRequestViewSet(viewsets.ModelViewSet):
    """/api/dashboard/intake-requests/ — public "New Project Request"
    submissions (ClientIntakeForm.jsx) and admin's Approve/Reject on
    them (ClientsPage.jsx Requests panel)."""

    queryset = IntakeRequest.objects.select_related("resulting_client").all()
    serializer_class = IntakeRequestSerializer
    # FIX: "delete" added — ClientsPage.jsx calls DELETE
    # /intake-requests/<id>/ after admin approves a request through the
    # Add Client modal (deleteIntakeRequest), and that was answered with
    # 405 every time, so an approved request stayed "pending" on the server.
    http_method_names = ["get", "post", "delete", "head", "options"]

    def get_queryset(self):
        # FIX (rejected/approved requests kept coming back in the Requests
        # panel): the frontend asks for ?status=pending, but this view
        # never read that param, so EVERY request (rejected + approved
        # ones too) was returned on each poll and re-appeared after being
        # declined. The status filter is now honoured.
        qs = super().get_queryset()
        status_param = self.request.query_params.get("status")
        if status_param:
            qs = qs.filter(status=status_param)
        return qs

    def get_permissions(self):
        # Anyone can submit the public intake form; everything else
        # (viewing the list, approving, rejecting) needs a logged-in
        # STAFF session -- these rows hold prospective clients' contact
        # details, budgets, etc., and approve/reject creates a real
        # Client row, so a client-role token must never reach this,
        # only IsAuthenticated was checked before (see employees.
        # permissions.IsStaff for the same "any staff, no client" rule
        # used by the Employees directory).
        if self.action == "create":
            return [permissions.AllowAny()]
        return [permissions.IsAuthenticated(), IsStaff()]

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        req = self.get_object()
        if req.status != "pending":
            return Response({"detail": "This request was already decided."}, status=status.HTTP_400_BAD_REQUEST)

        # Local imports to avoid a circular import (projects/tasks import
        # dashboard.Client at module load time).
        from projects.models import Project, Module
        from tasks.models import Task

        client = Client.objects.create(
            name=req.company_name or req.contact_person,
            email=req.email,
            contact_person=req.contact_person,
            phone=req.phone,
            city=req.city,
            country="Pakistan",
            created_by=request.user,
        )

        project = Project.objects.create(
            name=req.project_name or f"{client.name} Project",
            project_type="client",
            client=client,
            budget=req.budget or 0,
            description=req.requirements,
            created_by=request.user,
        )

        module_names = PROJECT_TYPE_MODULES.get(req.project_type, PROJECT_TYPE_MODULES["Other"])
        for i, name in enumerate(module_names):
            module = Module.objects.create(project=project, name=name, unlocked=(i == 0))
            if i == 0:
                Task.objects.create(
                    title=name,
                    project=project.name,
                    client=client,
                    status="Pending",
                    locked=False,
                    module_name=name,
                    module_project_name=project.name,
                    from_client_module=True,
                    requires_link=name in LINK_REQUIRED_MODULES,
                    # FIX: same key-shape mismatch as accept() above —
                    # must use the project's NAME, not its id, to match
                    # TasksPage.jsx's own moduleTaskKey().
                    module_task_key=f"{client.id}::{project.name}::{module.id}::",
                )

        req.status = "approved"
        req.resulting_client = client
        req.save()
        return Response(ClientSerializer(client).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        req = self.get_object()
        if req.status != "pending":
            return Response({"detail": "This request was already decided."}, status=status.HTTP_400_BAD_REQUEST)
        req.status = "rejected"
        req.save()
        return Response(IntakeRequestSerializer(req).data)

class ActivityLogEntryViewSet(viewsets.ReadOnlyModelViewSet):
    """/api/dashboard/activity/?client=<id> — the feed ClientPortal.jsx's
    Activity tab reads. Entries are written automatically at the
    transitions above (payment submitted/confirmed, module request
    accepted/rejected) — there's no direct-write endpoint."""

    queryset = ActivityLogEntry.objects.select_related("client").all()
    serializer_class = ActivityLogEntrySerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user
        if getattr(user, "role", None) == "client":
            client_profile = getattr(user, "client_profile", None)
            return qs.filter(client=client_profile) if client_profile else qs.none()
        client_id = self.request.query_params.get("client")
        if client_id:
            qs = qs.filter(client_id=client_id)
        return qs


class ClientMessageViewSet(viewsets.ModelViewSet):
    """/api/dashboard/messages/?client=<id> — ClientPortal.jsx's Messages
    tab: a client sees + posts to their own thread; staff can see any
    client's thread (?client=<id>) and reply into it. No edit/delete —
    a message thread is an append-only log."""

    queryset = ClientMessage.objects.select_related("client").all()
    serializer_class = ClientMessageSerializer
    permission_classes = [permissions.IsAuthenticated]
    http_method_names = ["get", "post", "head", "options"]

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user
        if getattr(user, "role", None) == "client":
            client_profile = getattr(user, "client_profile", None)
            return qs.filter(client=client_profile) if client_profile else qs.none()
        client_id = self.request.query_params.get("client")
        if client_id:
            qs = qs.filter(client_id=client_id)
        return qs

    def perform_create(self, serializer):
        user = self.request.user
        if getattr(user, "role", None) == "client":
            client_profile = getattr(user, "client_profile", None)
            if not client_profile:
                raise PermissionDenied("Portal access isn't linked to a client account.")
            serializer.save(client=client_profile, sender="client")
        else:
            serializer.save(sender="admin", created_by=user)


class SupportRequestView(APIView):
    """POST /api/dashboard/support/  { subject, message } — client-only.
    Logs into the same message thread + activity feed as everything
    else, and emails the support inbox when SMTP creds are configured."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        client = getattr(request.user, "client_profile", None)
        if not client:
            return Response({"detail": "Only clients can submit a support request."}, status=status.HTTP_403_FORBIDDEN)

        subject = (request.data.get("subject") or "").strip()
        message = (request.data.get("message") or "").strip()
        if not message:
            return Response({"detail": "Please describe what's going on."}, status=status.HTTP_400_BAD_REQUEST)

        ClientMessage.objects.create(client=client, sender="client", kind="support", subject=subject, text=message)
        log_activity(client, "Support request sent")

        support_email = getattr(dj_settings, "SUPPORT_EMAIL", dj_settings.EMAIL_HOST_USER)
        if dj_settings.EMAIL_HOST_USER and support_email:
            try:
                send_mail(
                    subject=f"[Support] {client.name} (#{client.id}) — {subject or 'No subject'}",
                    message=message,
                    from_email=dj_settings.EMAIL_HOST_USER,
                    recipient_list=[support_email],
                    fail_silently=True,
                )
            except Exception:
                pass

        return Response({"detail": "Support request sent."}, status=status.HTTP_201_CREATED)


class DocumentViewSet(viewsets.ModelViewSet):
    """/api/dashboard/documents/ — upload, list, view and delete client
    documents.

    Staff can:
      GET  /documents/                    — all documents
      GET  /documents/?client=<id>        — filter by client
      POST /documents/                    — upload (multipart: file, client, [project])
      GET  /documents/<id>/               — retrieve one
      DELETE /documents/<id>/             — delete (also removes file from disk)

    Clients (role="client") can:
      GET  /documents/                    — their own documents only (server-scoped)
      POST /documents/                    — upload a document for themselves
      GET  /documents/<id>/               — their own doc only
      DELETE /documents/<id>/             — their own doc only

    No PUT/PATCH — documents are immutable once uploaded.
    """

    queryset = Document.objects.select_related(
        "client", "project", "uploaded_by"
    ).all()
    serializer_class = DocumentSerializer
    permission_classes = [permissions.IsAuthenticated]
    http_method_names = ["get", "post", "delete", "head", "options"]

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user

        # Clients may only ever see their own documents — not another
        # client's, regardless of what ?client= says.
        if getattr(user, "role", None) == "client":
            # getattr alone doesn't catch RelatedObjectDoesNotExist that
            # Django raises for a reverse OneToOneField with no matching
            # row — use a try/except to turn that into a safe None.
            try:
                client_profile = user.client_profile
            except Exception:
                client_profile = None
            if not client_profile:
                return qs.none()
            return qs.filter(client=client_profile)

        # Staff: optional ?client=<id> filter.
        client_id = self.request.query_params.get("client")
        if client_id:
            qs = qs.filter(client_id=client_id)
        return qs

    def perform_create(self, serializer):
        user = self.request.user
        file = self.request.FILES.get("file")

        # If uploading as a client, force client to their own profile —
        # they can't supply a different client id.
        if getattr(user, "role", None) == "client":
            try:
                client_profile = user.client_profile
            except Exception:
                client_profile = None
            if not client_profile:
                raise PermissionDenied("No client profile associated with this account.")
            client = client_profile
        else:
            # Staff: 'client' field is required in the request body.
            client = serializer.validated_data.get("client")

        # Compute a human-readable file size from the actual uploaded file.
        file_size = ""
        if file and hasattr(file, "size"):
            size_bytes = file.size
            if size_bytes < 1024:
                file_size = f"{size_bytes} B"
            elif size_bytes < 1024 * 1024:
                file_size = f"{size_bytes / 1024:.1f} KB"
            else:
                file_size = f"{size_bytes / (1024 * 1024):.1f} MB"

        # file_name: prefer what the client sent, fall back to the actual
        # filename so the serializer always has something meaningful.
        file_name = (
            serializer.validated_data.get("file_name")
            or (file.name if file else "")
        )

        save_kwargs = {
            "uploaded_by": user if user.is_authenticated else None,
            "file_size": file_size,
            "file_name": file_name,
        }
        # FIX: always pass `client` explicitly for BOTH staff and client paths
        # so it is never left to DRF's validated_data merge (which is fragile
        # when the field is marked required=False in extra_kwargs and could be
        # silently dropped on some DRF versions).
        if client is not None:
            save_kwargs["client"] = client

        serializer.save(**save_kwargs)

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        # Delete the file from disk before removing the DB row.
        if instance.file:
            try:
                instance.file.delete(save=False)
            except Exception:
                pass  # missing file on disk is not a fatal error
        return super().destroy(request, *args, **kwargs)