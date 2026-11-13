from rest_framework.routers import SimpleRouter

from payments.views import (
    PaymentAllocationViewSet,
    PaymentViewSet,
    RefundViewSet,
    StudentRefundableViewSet,
)

router = SimpleRouter()
router.register("payments", PaymentViewSet, basename="payment")
router.register("payment-allocations", PaymentAllocationViewSet, basename="payment-allocation")
router.register("refunds", RefundViewSet, basename="refund")
# GET /students/{id}/refundable/ lives here: billing (which serves the other student account
# views) may not import payments
router.register("students", StudentRefundableViewSet, basename="student-refundable")
urlpatterns = router.urls
