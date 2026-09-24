from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .views import (
    DashboardStatsView,
    MostOrdersByCountryView,
    LowPurchaseClientsView,
    CustomerSatisfactionView,
    UserGrowthView,
    SalesOverviewView,
    DashboardNotificationsView,
    ClientViewSet,
    OrderViewSet,
    ExpenseViewSet,
    IncomeViewSet,
    InvoiceViewSet,
    ModuleRequestViewSet,
    IntakeRequestViewSet,
    ActivityLogEntryViewSet,
    ClientMessageViewSet,
    SupportRequestView,
    DocumentViewSet,
)

router = DefaultRouter()
router.register(r"clients", ClientViewSet, basename="client")
router.register(r"orders", OrderViewSet, basename="order")
router.register(r"expenses", ExpenseViewSet, basename="expense")
router.register(r"incomes", IncomeViewSet, basename="income")
router.register(r"invoices", InvoiceViewSet, basename="invoice")
router.register(r"module-requests", ModuleRequestViewSet, basename="module-request")
router.register(r"intake-requests", IntakeRequestViewSet, basename="intake-request")
router.register(r"activity", ActivityLogEntryViewSet, basename="activity")
router.register(r"messages", ClientMessageViewSet, basename="client-message")
router.register(r"documents", DocumentViewSet, basename="document")

urlpatterns = [
    path("stats/", DashboardStatsView.as_view(), name="dashboard-stats"),
    path("most-orders-by-country/", MostOrdersByCountryView.as_view(), name="most-orders-by-country"),
    path("low-purchase-clients/", LowPurchaseClientsView.as_view(), name="low-purchase-clients"),
    path("customer-satisfaction/", CustomerSatisfactionView.as_view(), name="customer-satisfaction"),
    path("user-growth/", UserGrowthView.as_view(), name="user-growth"),
    path("sales-overview/", SalesOverviewView.as_view(), name="sales-overview"),
    path("notifications/", DashboardNotificationsView.as_view(), name="dashboard-notifications"),
    path("support/", SupportRequestView.as_view(), name="client-support"),
    path("", include(router.urls)),
]