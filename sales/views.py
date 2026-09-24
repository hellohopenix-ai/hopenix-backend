from django.db.models import Q
from rest_framework import viewsets, permissions

from users.access import ModuleAccess

from .models import Sale
from .serializers import SaleSerializer


class SaleViewSet(viewsets.ModelViewSet):
    """Full CRUD for SalesPage.jsx (list/create/retrieve/update/delete).

    The frontend fetches the full list once and does its own filtering,
    sorting, pagination and summary math client-side (see
    filteredSales / pageRows / STAT_CARDS in SalesPage.jsx) exactly like
    it did against localStorage before — only the source of `sales`
    changed. The query params below are optional extras for anyone
    calling the API directly (e.g. a report tool), same pattern as
    TaskViewSet.get_queryset.
    """

    queryset = Sale.objects.select_related("created_by").all()
    serializer_class = SaleSerializer
    # Server-side enforcement of the Page Access / Module Access tables for
    # the "Sales" page (see users/access.py): the caller must be allowed to
    # open the Sales page AND hold the matching view/create/edit/delete flag.
    # Client-role tokens are always refused.
    module_name = "Sales"
    permission_classes = [permissions.IsAuthenticated, ModuleAccess]

    def get_queryset(self):
        qs = super().get_queryset()
        params = self.request.query_params

        client = params.get("client")
        if client:
            qs = qs.filter(client=client)

        project = params.get("project")
        if project:
            qs = qs.filter(project=project)

        status_param = params.get("status")
        if status_param:
            qs = qs.filter(status=status_param)

        date_from = params.get("dateFrom")
        if date_from:
            qs = qs.filter(date__gte=date_from)

        date_to = params.get("dateTo")
        if date_to:
            qs = qs.filter(date__lte=date_to)

        search = params.get("search")
        if search:
            qs = qs.filter(Q(client__icontains=search) | Q(project__icontains=search))

        return qs

    def perform_create(self, serializer):
        user = self.request.user
        serializer.save(created_by=user if user and user.is_authenticated else None)