from rest_framework.routers import SimpleRouter

from reconciliation.views import (
    AnomalyFindingViewSet,
    BalanceSnapshotViewSet,
    ReconciliationRunViewSet,
)

router = SimpleRouter()
router.register("balance-snapshots", BalanceSnapshotViewSet, basename="balance-snapshot")
router.register("reconciliation/runs", ReconciliationRunViewSet, basename="reconciliation-run")
router.register("anomalies", AnomalyFindingViewSet, basename="anomaly")
urlpatterns = router.urls
