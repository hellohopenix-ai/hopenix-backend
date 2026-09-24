from django.db.models import Q, Sum
from rest_framework import permissions, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from users.access import ModuleAccess

from .models import Expense
from .permissions import can_delete_expense, can_edit_expense
from .serializers import ExpenseSerializer


class ExpenseViewSet(viewsets.ModelViewSet):
    """Full CRUD for ExpensesPage.jsx, plus the extra bits the page
    needs beyond plain field edits: receipt upload/replace/remove, a
    `summary` endpoint for the stat cards + category breakdown, and a
    `filters` endpoint for the category/project dropdown options —
    all three currently computed client-side from the *entire*
    localStorage array, which won't scale once data is real and shared.

    Server-side enforcement of the Page Access / Module Access tables for
    the "Expenses" page (see users/access.py): the caller must be allowed
    to open the Expenses page AND hold the matching view/create/edit/
    delete flag. Client-role tokens are always refused.
    """

    module_name = "Expenses"
    serializer_class = ExpenseSerializer
    permission_classes = [permissions.IsAuthenticated, ModuleAccess]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get_queryset(self):
        qs = Expense.objects.select_related("created_by").all()
        params = self.request.query_params

        category = params.get("category")
        if category and category != "All Categories":
            qs = qs.filter(category=category)

        project = params.get("project")
        if project and project != "All Projects":
            qs = qs.filter(project=project)

        payment = params.get("payment")
        if payment and payment != "All Payment Methods":
            qs = qs.filter(payment=payment)

        status_param = params.get("status")
        if status_param and status_param != "All Statuses":
            qs = qs.filter(status=status_param)

        search = params.get("search")
        if search:
            qs = qs.filter(Q(title__icontains=search) | Q(project__icontains=search))

        date_from = params.get("dateFrom")
        if date_from:
            qs = qs.filter(date__gte=date_from)
        date_to = params.get("dateTo")
        if date_to:
            qs = qs.filter(date__lte=date_to)

        return qs

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

    def perform_update(self, serializer):
        if not can_edit_expense(self.request.user, self.get_object()):
            raise PermissionDenied("You can't edit this expense.")
        serializer.save()

    def perform_destroy(self, instance):
        if not can_delete_expense(self.request.user, instance):
            raise PermissionDenied("You can't delete this expense.")
        if instance.receipt_file:
            instance.receipt_file.delete(save=False)
        instance.delete()

    # -- Receipt upload / replace / remove -------------------------------
    # Mirrors handleReceiptUpload (POST, new or replace) and
    # handleFormReceiptClear (DELETE) on the frontend.
    @action(detail=True, methods=["post", "delete"], url_path="receipt")
    def receipt(self, request, pk=None):
        expense = self.get_object()
        if not can_edit_expense(request.user, expense):
            return Response({"error": "Not authorized."}, status=403)

        if request.method == "DELETE":
            if expense.receipt_file:
                expense.receipt_file.delete(save=False)
            expense.receipt_file = None
            expense.receipt_name = ""
            expense.receipt_content_type = ""
            expense.save(update_fields=["receipt_file", "receipt_name", "receipt_content_type", "updated_at"])
            return Response(ExpenseSerializer(expense, context={"request": request}).data)

        file_obj = request.FILES.get("receipt")
        if not file_obj:
            return Response({"error": "No file uploaded."}, status=400)
        if file_obj.content_type != "application/pdf" and not file_obj.content_type.startswith("image/"):
            return Response({"error": "Receipt must be an image or a PDF."}, status=400)

        if expense.receipt_file:
            expense.receipt_file.delete(save=False)

        expense.receipt_file = file_obj
        expense.receipt_name = file_obj.name
        expense.receipt_content_type = file_obj.content_type
        expense.save(update_fields=["receipt_file", "receipt_name", "receipt_content_type", "updated_at"])
        return Response(ExpenseSerializer(expense, context={"request": request}).data, status=201)

    # -- Stat cards + category breakdown ---------------------------------
    @action(detail=False, methods=["get"])
    def summary(self, request):
        """GET /api/expenses/expenses/summary/ — always over ALL
        expenses (ignores the list's filters), same as the frontend's
        own totalAmount/categoryTotals which are built from `expenses`,
        not `filteredExpenses`."""

        qs = Expense.objects.all()
        total_amount = qs.aggregate(s=Sum("amount"))["s"] or 0

        pending_qs = qs.filter(status="Pending")
        pending_amount = pending_qs.aggregate(s=Sum("amount"))["s"] or 0

        by_category = qs.values("category").annotate(amount=Sum("amount")).order_by("-amount")
        denom = float(total_amount) or 1
        category_totals = [
            {
                "name": row["category"],
                "amount": float(row["amount"]),
                "pct": round((float(row["amount"]) / denom) * 100),
            }
            for row in by_category
        ]

        return Response({
            "totalAmount": float(total_amount),
            "pendingAmount": float(pending_amount),
            "pendingCount": pending_qs.count(),
            "categoriesCount": len(category_totals),
            "categoryTotals": category_totals,
            "approvedAmount": float(qs.filter(status="Approved").aggregate(s=Sum("amount"))["s"] or 0),
            "rejectedAmount": float(qs.filter(status="Rejected").aggregate(s=Sum("amount"))["s"] or 0),
        })

    # -- Filter dropdown options ------------------------------------------
    @action(detail=False, methods=["get"])
    def filters(self, request):
        """GET /api/expenses/expenses/filters/ — distinct category and
        project values actually in the table right now, replacing the
        frontend's allCategoryNames/projectOptions (derived from the
        in-memory `expenses` array)."""

        categories = list(
            Expense.objects.order_by("category").values_list("category", flat=True).distinct()
        )
        projects = list(
            Expense.objects.exclude(project="").order_by("project").values_list("project", flat=True).distinct()
        )
        return Response({"categories": categories, "projects": projects})