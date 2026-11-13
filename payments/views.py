"""Payments and refunds API (ARCHITECTURE §9.2, Payments and refunds). Paths exactly as
documented; financial resources are read + action only (no PUT / PATCH / DELETE, §9.1).

Permissions (ARCHITECTURE §13, decision D-P3): platform_admin records and allocates payments
and approves, processes and rejects refunds; support_staff reads everything and may request a
refund (POST /refunds/); everyone else, learners included, has no access.
"""

from django.db.models import Prefetch
from django.shortcuts import get_object_or_404
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import OpenApiExample, extend_schema, extend_schema_view
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.filters import OrderingFilter
from rest_framework.response import Response

from core.permissions import (
    BILLING_READ,
    BILLING_WRITE,
    PLATFORM_ADMIN,
    SUPPORT_STAFF,
    ReadWriteRoles,
)
from core.schema import INVALID_PAGE, error_responses
from payments import openapi as ex
from payments import services
from payments.filters import PaymentAllocationFilter, PaymentFilter, RefundFilter
from payments.models import Payment, PaymentAllocation, Refund
from payments.serializers import (
    AllocateResponseSerializer,
    AllocateSerializer,
    PaymentAllocationSerializer,
    PaymentCreatedSerializer,
    PaymentCreateSerializer,
    PaymentDetailSerializer,
    PaymentSerializer,
    RefundableSerializer,
    RefundCreateSerializer,
    RefundDetailSerializer,
    RefundProcessSerializer,
    RefundSerializer,
    RefundStatusDateSerializer,
)
from students.models import Student

ID_LOOKUP = r"[^/]+"
REFUND_REQUEST_ROLES = (PLATFORM_ADMIN, SUPPORT_STAFF)  # §13: support staff request refunds


def _actor(request) -> str:
    return request.user.get_username()


def _payments():
    return Payment.objects.prefetch_related(
        Prefetch("allocations", queryset=PaymentAllocation.objects.select_related("payment")),
        "refunds",
    )


class _PaymentsPermissions:
    permission_classes = [ReadWriteRoles]
    read_roles = BILLING_READ
    write_roles = BILLING_WRITE
    lookup_value_regex = ID_LOOKUP
    http_method_names = ["get", "post", "head", "options"]
    filter_backends = [DjangoFilterBackend, OrderingFilter]


@extend_schema_view(
    list=extend_schema(
        summary="List payments",
        tags=["payments"],
        responses={200: PaymentSerializer(many=True), **ex.PAYMENT_LIST_ERRORS},
    ),
    retrieve=extend_schema(
        summary="Get a payment with its allocations",
        tags=["payments"],
        responses={
            200: PaymentDetailSerializer,
            **error_responses(401, 403, 404, examples={404: ex.PAYMENT_404}),
        },
    ),
    create=extend_schema(
        summary="Record a payment",
        description=(
            "Records the payment and its PAYMENT ledger entry (−amount, dated received_date): "
            "a payment is credited in full when received (BR-PAY-02). Any payer counts as "
            "money paid (BR-PAY-03). external_reference is unique: resubmitting returns 409 "
            "DUPLICATE_PAYMENT (BR-PAY-01). received_date must be a 2026 business date on or "
            "after the student's first invoice issue date. With auto_allocate=true the payment "
            "is AUTO-allocated in the same transaction (see the allocate endpoint)."
        ),
        tags=["payments"],
        request=PaymentCreateSerializer,
        responses={
            201: PaymentCreatedSerializer,
            **error_responses(
                400,
                401,
                403,
                409,
                examples={400: ex.PAYMENT_CREATE_400, 409: ex.PAYMENT_CREATE_409},
            ),
        },
        examples=[
            OpenApiExample(
                "Guardian, partial, AUTO",
                request_only=True,
                value={
                    "student_id": "STU-000001",
                    "amount": "60000.00",
                    "received_date": "2026-08-25",
                    "method": "BANK_TRANSFER",
                    "payer_type": "GUARDIAN",
                    "payer_name": "R. Rao",
                    "external_reference": "NEFT-2026-0825-001",
                    "auto_allocate": True,
                },
            )
        ],
    ),
    allocate=extend_schema(
        summary="Allocate a payment to invoice lines",
        description=(
            "AUTO: allocates everything available, oldest invoice due_date first, then line "
            "allocation_priority, then line_id (BR-ALC-03), never beyond a line's open amount "
            "(BR-ALC-02). The remainder stays unallocated credit (BR-ALC-04). With no open "
            "line, or nothing available, nothing is written and `outcome` says why "
            "(NO_OPEN_LINES / NO_FUNDS_AVAILABLE): neither is an error. allocated_on defaults "
            "to the payment's received_date. MANUAL: `lines` (1–99 distinct FEE or LATE_FEE "
            "lines of the same student) and `allocated_on` are required; all-or-nothing; 422 "
            "over the payment's available amount (BR-ALC-01) or a line's open amount "
            "(BR-ALC-02). Allocations post nothing to the ledger; collection statuses are "
            "refreshed. A payment holds at most 99 allocation rows, deallocations included "
            "(409 ALLOCATION_LIMIT_REACHED, an approved exception to the 18-code catalogue)."
        ),
        tags=["payments"],
        request=AllocateSerializer,
        responses={
            200: AllocateResponseSerializer,
            **error_responses(
                400,
                401,
                403,
                404,
                409,
                422,
                examples={
                    400: ex.ALLOCATE_400,
                    404: ex.PAYMENT_404,
                    409: ex.ALLOCATE_409,
                    422: ex.ALLOCATE_422,
                },
            ),
        },
        examples=[
            OpenApiExample("AUTO", request_only=True, value={"mode": "AUTO"}),
            OpenApiExample(
                "MANUAL",
                request_only=True,
                value={
                    "mode": "MANUAL",
                    "allocated_on": "2026-08-25",
                    "lines": [{"invoice_line_id": f"{ex.INV}-L02", "amount": "6000.00"}],
                },
            ),
        ],
    ),
    allocations=extend_schema(
        summary="A payment's allocation rows",
        description="ALLOCATE and DEALLOCATE rows (signed), by allocation_id.",
        tags=["payments"],
        responses={
            200: PaymentAllocationSerializer(many=True),
            **error_responses(401, 403, 404, examples={404: [ex.PAYMENT_404, INVALID_PAGE]}),
        },
    ),
)
class PaymentViewSet(
    _PaymentsPermissions,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    queryset = _payments()
    serializer_class = PaymentSerializer
    filterset_class = PaymentFilter
    ordering_fields = ["payment_id", "received_date"]
    ordering = ["payment_id"]

    def get_serializer_class(self):
        return PaymentDetailSerializer if self.action == "retrieve" else PaymentSerializer

    def create(self, request, *args, **kwargs):
        payload = PaymentCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = dict(payload.validated_data)
        payment, result = services.record_payment(
            student=data.pop("student_id"), actor=_actor(request), **data
        )
        body = PaymentCreatedSerializer(
            _payments().get(pk=payment.pk), context={"auto_allocation": result}
        ).data
        return Response(body, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def allocate(self, request, pk=None):
        payment = self.get_object()
        payload = AllocateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        result = services.allocate_payment(
            payment,
            mode=data["mode"],
            lines=[(x["invoice_line_id"], x["amount"]) for x in data.get("lines") or []],
            allocated_on=data.get("allocated_on"),
            actor=_actor(request),
        )
        body = AllocateResponseSerializer(
            {
                "mode": result.mode,
                "outcome": result.outcome,
                "allocated_on": result.allocated_on,
                "available_before": result.available_before,
                "allocated": result.allocated,
                "unallocated_after": result.unallocated_after,
                "allocations": result.allocations,
                "payment": _payments().get(pk=payment.pk),
            }
        ).data
        return Response(body)

    @action(detail=True, methods=["get"], filter_backends=[])
    def allocations(self, request, pk=None):
        payment = self.get_object()
        rows = PaymentAllocation.objects.filter(payment=payment).select_related(
            "payment", "invoice_line"
        )
        page = self.paginate_queryset(rows.order_by("allocation_id"))
        return self.get_paginated_response(PaymentAllocationSerializer(page, many=True).data)


@extend_schema_view(
    list=extend_schema(
        summary="List payment allocations (all payments)",
        description="ALLOCATE and DEALLOCATE rows, including releases (rule_applied).",
        tags=["payments"],
        responses={200: PaymentAllocationSerializer(many=True), **ex.ALLOCATION_LIST_ERRORS},
    ),
)
class PaymentAllocationViewSet(
    _PaymentsPermissions, mixins.ListModelMixin, viewsets.GenericViewSet
):
    queryset = PaymentAllocation.objects.select_related("payment", "invoice_line")
    serializer_class = PaymentAllocationSerializer
    filterset_class = PaymentAllocationFilter
    ordering_fields = ["allocation_id", "allocated_on"]
    ordering = ["allocation_id"]


@extend_schema_view(
    list=extend_schema(
        summary="List refunds",
        tags=["refunds"],
        responses={200: RefundSerializer(many=True), **ex.REFUND_LIST_ERRORS},
    ),
    retrieve=extend_schema(
        summary="Get a refund with its status history",
        tags=["refunds"],
        responses={
            200: RefundDetailSerializer,
            **error_responses(401, 403, 404, examples={404: ex.REFUND_404}),
        },
    ),
    create=extend_schema(
        summary="Request a refund",
        description=(
            "∅ → REQUESTED, from one source payment, back to its payer (BR-REF-04). Three caps "
            "(BR-REF-01/02), checked in this order: net paid (Σ payments − Σ refunds not "
            "REJECTED); the credit available (−ledger balance when negative, less what pending "
            "REQUESTED / APPROVED refunds reserve); the payment's available amount (amount − "
            "net allocations − refunds not REJECTED). The figures are stored as `calculation`, "
            "the request-time audit snapshot. Platform admins and support staff may request. "
            "reason is OVERPAYMENT (withdrawal and cancellation refunds come with Phase 5)."
        ),
        tags=["refunds"],
        request=RefundCreateSerializer,
        responses={
            201: RefundDetailSerializer,
            **error_responses(
                400, 401, 403, 422, examples={400: ex.REFUND_CREATE_400, 422: ex.REFUND_CREATE_422}
            ),
        },
        examples=[
            OpenApiExample(
                "Overpayment",
                request_only=True,
                value={
                    "payment_id": "PAY-2026-000002",
                    "amount": "13000.00",
                    "requested_date": "2026-09-18",
                },
            )
        ],
    ),
    approve=extend_schema(
        summary="Approve a refund",
        description=(
            "REQUESTED → APPROVED on on_date. The caps are checked again now, leaving this "
            "refund out of the reservations it already holds; `calculation` keeps the "
            "request-time snapshot."
        ),
        tags=["refunds"],
        request=RefundStatusDateSerializer,
        responses={
            200: RefundDetailSerializer,
            **error_responses(
                400,
                401,
                403,
                404,
                409,
                422,
                examples={
                    400: ex.APPROVE_400,
                    404: ex.REFUND_404,
                    409: ex.APPROVE_409,
                    422: ex.APPROVE_422,
                },
            ),
        },
        examples=[OpenApiExample("Approve", request_only=True, value={"on_date": "2026-09-20"})],
    ),
    process=extend_schema(
        summary="Process (pay out) an approved refund",
        description=(
            "APPROVED → PROCESSED on processed_date (≤ 2026-12-31). The caps are checked again; "
            "the REFUND ledger entry (+amount) is posted in the same transaction (BR-REF-03)."
        ),
        tags=["refunds"],
        request=RefundProcessSerializer,
        responses={
            200: RefundDetailSerializer,
            **error_responses(
                400,
                401,
                403,
                404,
                409,
                422,
                examples={
                    400: ex.PROCESS_400,
                    404: ex.REFUND_404,
                    409: ex.PROCESS_409,
                    422: ex.PROCESS_422,
                },
            ),
        },
        examples=[
            OpenApiExample("Process", request_only=True, value={"processed_date": "2026-09-30"})
        ],
    ),
    reject=extend_schema(
        summary="Reject a refund",
        description="REQUESTED / APPROVED → REJECTED on on_date; the reservation is released.",
        tags=["refunds"],
        request=RefundStatusDateSerializer,
        responses={
            200: RefundDetailSerializer,
            **error_responses(
                400,
                401,
                403,
                404,
                409,
                examples={400: ex.REJECT_400, 404: ex.REFUND_404, 409: ex.REJECT_409},
            ),
        },
        examples=[OpenApiExample("Reject", request_only=True, value={"on_date": "2026-09-20"})],
    ),
)
class RefundViewSet(
    _PaymentsPermissions,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    queryset = Refund.objects.prefetch_related("events")
    serializer_class = RefundSerializer
    filterset_class = RefundFilter
    ordering_fields = ["refund_id", "requested_date"]
    ordering = ["refund_id"]

    @property
    def write_roles(self):
        return REFUND_REQUEST_ROLES if self.action == "create" else BILLING_WRITE

    def get_serializer_class(self):
        return RefundSerializer if self.action == "list" else RefundDetailSerializer

    def _detail(self, pk):
        return RefundDetailSerializer(Refund.objects.prefetch_related("events").get(pk=pk)).data

    def create(self, request, *args, **kwargs):
        payload = RefundCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        refund = services.request_refund(
            payment=data["payment_id"],
            amount=data["amount"],
            requested_date=data["requested_date"],
            reason=data["reason"],
            actor=_actor(request),
        )
        return Response(self._detail(refund.pk), status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        refund = self.get_object()
        payload = RefundStatusDateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        services.approve_refund(
            refund, on_date=payload.validated_data["on_date"], actor=_actor(request)
        )
        return Response(self._detail(refund.pk))

    @action(detail=True, methods=["post"])
    def process(self, request, pk=None):
        refund = self.get_object()
        payload = RefundProcessSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        services.process_refund(
            refund,
            processed_date=payload.validated_data["processed_date"],
            actor=_actor(request),
        )
        return Response(self._detail(refund.pk))

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        refund = self.get_object()
        payload = RefundStatusDateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        services.reject_refund(
            refund, on_date=payload.validated_data["on_date"], actor=_actor(request)
        )
        return Response(self._detail(refund.pk))


@extend_schema_view(
    refundable=extend_schema(
        summary="How much can be refunded to a student",
        description=(
            "max_refundable = max_refundable(net paid, credit available, Σ payments' available) "
            "and, per payment, its own maximum (§7 Stage C). Net paid subtracts every refund "
            "not REJECTED; the credit available is the ledger credit balance less the "
            "REQUESTED / APPROVED refunds that reserve it."
        ),
        tags=["refunds"],
        responses={
            200: RefundableSerializer,
            **error_responses(401, 403, 404, examples={404: ex.STUDENT_404}),
        },
    ),
)
class StudentRefundableViewSet(_PaymentsPermissions, viewsets.GenericViewSet):
    queryset = Student.objects.all()
    serializer_class = RefundableSerializer
    http_method_names = ["get", "head", "options"]
    filter_backends: list = []

    @action(detail=True, methods=["get"], pagination_class=None)
    def refundable(self, request, pk=None):
        student = get_object_or_404(Student, pk=pk)
        return Response(RefundableSerializer(services.refundable_summary(student)).data)
