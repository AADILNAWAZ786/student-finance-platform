from django.urls import path
from rest_framework.routers import SimpleRouter

from billing.views import (
    CollectionRefreshView,
    FeeScheduleViewSet,
    InvoiceViewSet,
    LateFeeAssessView,
    StudentAccountViewSet,
)

router = SimpleRouter()
router.register("fee-schedules", FeeScheduleViewSet, basename="fee-schedule")
router.register("invoices", InvoiceViewSet, basename="invoice")
# /students/{id}/ledger|statement|balance/ (documented paths; implemented in billing, D7)
router.register("students", StudentAccountViewSet, basename="student-account")

urlpatterns = [
    *router.urls,
    path("billing/late-fees/assess/", LateFeeAssessView.as_view(), name="late-fees-assess"),
    path(
        "billing/collection-status/refresh/",
        CollectionRefreshView.as_view(),
        name="collection-status-refresh",
    ),
]
