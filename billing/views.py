"""Billing API (ARCHITECTURE §9.2, Billing; and the student ledger/statement/balance views).

Financial resources are read + action only: there is no PUT, PATCH or DELETE. Fee
schedules have no detail route (decision D14); ``/fee-schedules/{id}/history/`` returns
every version with its derived window. Permissions follow decision D6.

The student account views live here, not in ``students``, because the dependency order
is students → academics → billing (decision D7); their paths are the documented ones.
"""

from django.shortcuts import get_object_or_404
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiParameter,
    extend_schema,
    extend_schema_view,
)
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.filters import OrderingFilter
from rest_framework.response import Response
from rest_framework.views import APIView

from billing import services
from billing.filters import FeeScheduleFilter, InvoiceFilter
from billing.models import FeeSchedule, Invoice, LedgerEntry
from billing.serializers import (
    BalanceQuerySerializer,
    BalanceSerializer,
    CollectionRefreshResultSerializer,
    CollectionRefreshSerializer,
    FeeScheduleCreateSerializer,
    FeeScheduleNewVersionSerializer,
    FeeScheduleSerializer,
    InvoiceDetailSerializer,
    InvoiceGenerateSerializer,
    InvoiceSerializer,
    InvoiceVoidSerializer,
    LateFeeAssessSerializer,
    LateFeeResultSerializer,
    LedgerEntrySerializer,
    StatementSerializer,
)
from core.permissions import BILLING_READ, BILLING_WRITE, ReadWriteRoles
from core.schema import (
    INVALID_PAGE,
    err,
    error_responses,
    invalid_choice,
    list_error_responses,
    not_found,
)
from students.models import Student

ID_LOOKUP = r"[^/]+"  # any path segment: malformed IDs reach DRF → enveloped 404
V1 = "FEE-PRG-001-2026-FALL-TUITION-DOM-V1"
V2 = "FEE-PRG-001-2026-FALL-TUITION-DOM-V2"
INV = "INV-STU-000001-2026-000001"
INVALID = "Request validation failed."

# --- error examples: each is exactly what its endpoint returns (replayed by
# tests/verification/test_verify_openapi_examples.py). No shared defaults.
FEE_LIST_ERRORS = list_error_responses(invalid_choice("fee_type", "PARKING"))
FEE_CREATE_400 = [
    (
        "Residency not allowed for the fee type",
        err(
            "VALIDATION_ERROR",
            INVALID,
            {"residency": ["TUITION fee schedules must use residency DOMESTIC or INTERNATIONAL."]},
        ),
    ),
    (
        "Server-controlled field sent",
        err(
            "VALIDATION_ERROR",
            INVALID,
            {"allocation_priority": ["Set by the server from fee_type; clients cannot send it."]},
        ),
    ),
]
FEE_CREATE_422 = (
    "Version 1 already exists",
    err(
        "FEE_SCHEDULE_VERSION_INVALID",
        "A fee schedule already exists for PRG-001, TERM-2026-FALL, TUITION, DOMESTIC. "
        f"Create a new version of {V1} instead.",
        {"latest_fee_schedule_id": V1},
        rule_id="BR-FEE-03",
    ),
)
FEE_NEW_VERSION_400 = (
    "Amount sent as a JSON number",
    err(
        "VALIDATION_ERROR", INVALID, {"amount": ['Money must be sent as a string, e.g. "1250.00".']}
    ),
)
FEE_NEW_VERSION_422 = [
    (
        "effective_from not after the predecessor",
        err(
            "FEE_SCHEDULE_VERSION_INVALID",
            f"effective_from must be later than 2026-01-01 (the effective_from of {V1}).",
            {
                "fee_schedule_id": V1,
                "predecessor_effective_from": "2026-01-01",
                "effective_from": "2026-01-01",
            },
            rule_id="BR-FEE-03",
        ),
    ),
    (
        "Not the latest version",
        err(
            "FEE_SCHEDULE_VERSION_INVALID",
            f"{V1} is not the latest version; the latest is {V2}.",
            {"fee_schedule_id": V1, "latest_fee_schedule_id": V2},
            rule_id="BR-FEE-03",
        ),
    ),
]
FEE_404 = not_found("FeeSchedule")

GENERATE_400 = [
    (
        "Waiver on a non-waivable fee",
        err(
            "VALIDATION_ERROR",
            INVALID,
            {"waivers": ["waivers[0] (TUITION): TUITION is not a waivable fee type."]},
        ),
    ),
    (
        "Unknown enrollment",
        err(
            "VALIDATION_ERROR",
            INVALID,
            {"enrollment_id": ['Invalid pk "ENR-2026-999999" - object does not exist.']},
        ),
    ),
]
GENERATE_409 = [
    (
        "Invoice already generated",
        err(
            "DUPLICATE_INVOICE",
            f"Enrollment ENR-2026-000001 already has invoice {INV}.",
            {"enrollment_id": "ENR-2026-000001", "existing_invoice_id": INV},
            rule_id="BR-INV-01",
        ),
    ),
    (
        "Enrollment not ENROLLED",
        err(
            "ILLEGAL_TRANSITION",
            "Enrollment ENR-2026-000002 is COMPLETED; an invoice can be generated only for an "
            "ENROLLED enrollment.",
            {
                "entity": "Enrollment",
                "record_id": "ENR-2026-000002",
                "status": "COMPLETED",
                "required_status": "ENROLLED",
            },
        ),
    ),
]
GENERATE_422 = (
    "No fee schedule in force",
    err(
        "FEE_SCHEDULE_NOT_FOUND",
        "No fee schedule version is in force on 2026-05-04 for TUITION, LAB, REGISTRATION "
        "(PRG-001, TERM-2026-SUMMER, DOMESTIC).",
        {
            "enrollment_id": "ENR-2026-000003",
            "program_id": "PRG-001",
            "term_id": "TERM-2026-SUMMER",
            "residency": "DOMESTIC",
            "on_date": "2026-05-04",
            "missing_fee_types": ["TUITION", "LAB", "REGISTRATION"],
        },
        rule_id="BR-FEE-01",
    ),
)
INVOICE_LIST_ERRORS = list_error_responses(invalid_choice("collection_status", "LATE"))
INVOICE_404 = not_found("Invoice")
VOID_400 = [
    (
        "void_date before the issue date",
        err(
            "VALIDATION_ERROR",
            INVALID,
            {"void_date": ["void_date must be on or after the invoice issue date (2026-08-20)."]},
        ),
    ),
    (
        "void_date missing",
        err("VALIDATION_ERROR", INVALID, {"void_date": ["This field is required."]}),
    ),
]
VOID_409 = [
    (
        "Already void",
        err(
            "ILLEGAL_TRANSITION",
            "Invoice cannot move from VOID to VOID.",
            {
                "entity": "Invoice",
                "record_id": INV,
                "from_status": "VOID",
                "to_status": "VOID",
                "allowed": [],
            },
            rule_id="BR-INV-08",
        ),
    ),
    (
        "Scholarship or payment applied",
        err(
            "INVOICE_HAS_ALLOCATIONS",
            f"{INV} has scholarship or payment applied; it cannot be voided.",
            {"invoice_id": INV, "scholarship_applied": "40000.00", "amount_paid": "0.00"},
            rule_id="BR-INV-08",
        ),
    ),
]
LATE_FEE_400 = (
    "as_of_date missing",
    err("VALIDATION_ERROR", INVALID, {"as_of_date": ["This field is required."]}),
)
REFRESH_400 = (
    "as_of_date outside 2026",
    err(
        "VALIDATION_ERROR",
        INVALID,
        {"as_of_date": ["Must be a business date between 2026-01-01 and 2026-12-31."]},
    ),
)
STUDENT_404 = not_found("Student")
BALANCE_400 = (
    "as_of not a date",
    err(
        "VALIDATION_ERROR",
        INVALID,
        {"as_of": ["Date has wrong format. Use one of these formats instead: YYYY-MM-DD."]},
    ),
)


def _actor(request) -> str:
    return request.user.get_username()


class _BillingPermissions:
    permission_classes = [ReadWriteRoles]
    read_roles = BILLING_READ
    write_roles = BILLING_WRITE
    lookup_value_regex = ID_LOOKUP
    http_method_names = ["get", "post", "head", "options"]


# --- fee schedules ---------------------------------------------------------------------------


@extend_schema_view(
    list=extend_schema(
        summary="List fee schedule versions",
        description="Filter with in_force_on=YYYY-MM-DD to get the version of each chain in "
        "force on that date. Every row is immutable (BR-FEE-03).",
        tags=["billing"],
        responses={200: FeeScheduleSerializer(many=True), **FEE_LIST_ERRORS},
    ),
    create=extend_schema(
        summary="Create version 1 of a fee schedule",
        description="Flags and allocation priority are set by the server from fee_type "
        "(D5). Residency: TUITION DOMESTIC or INTERNATIONAL, INTERNATIONAL_SERVICE "
        "INTERNATIONAL, every other fee type ALL (D3). A later price is a new version.",
        tags=["billing"],
        request=FeeScheduleCreateSerializer,
        responses={
            201: FeeScheduleSerializer,
            **error_responses(
                400, 401, 403, 422, examples={400: FEE_CREATE_400, 422: FEE_CREATE_422}
            ),
        },
        examples=[
            OpenApiExample(
                "Domestic tuition",
                request_only=True,
                value={
                    "program": "PRG-001",
                    "term": "TERM-2026-FALL",
                    "fee_type": "TUITION",
                    "residency": "DOMESTIC",
                    "charge_basis": "PER_CREDIT",
                    "amount": "9000.00",
                    "effective_from": "2026-01-01",
                },
            )
        ],
    ),
    new_version=extend_schema(
        summary="Insert the next version of a fee schedule",
        description="Supersedes the given (latest) version; the old row is untouched. "
        "effective_from must be later than the predecessor's (BR-FEE-03).",
        tags=["billing"],
        request=FeeScheduleNewVersionSerializer,
        responses={
            201: FeeScheduleSerializer,
            **error_responses(
                400,
                401,
                403,
                404,
                422,
                examples={400: FEE_NEW_VERSION_400, 404: FEE_404, 422: FEE_NEW_VERSION_422},
            ),
        },
        examples=[
            OpenApiExample(
                "FALL revision",
                request_only=True,
                value={"amount": "9360.00", "effective_from": "2026-08-01"},
            )
        ],
    ),
    history=extend_schema(
        summary="Version history of a fee schedule",
        description="Every version of the chain, oldest first, with derived effective_to.",
        tags=["billing"],
        responses={
            200: FeeScheduleSerializer(many=True),
            **error_responses(401, 403, 404, examples={404: FEE_404}),
        },
    ),
)
class FeeScheduleViewSet(
    _BillingPermissions, mixins.ListModelMixin, mixins.CreateModelMixin, viewsets.GenericViewSet
):
    queryset = FeeSchedule.objects.select_related("superseded_by")
    serializer_class = FeeScheduleSerializer
    filterset_class = FeeScheduleFilter
    filter_backends = [DjangoFilterBackend, OrderingFilter]
    ordering_fields = ["fee_schedule_id", "effective_from", "version"]
    ordering = ["program_id", "term_id", "fee_type", "residency", "version"]

    def create(self, request, *args, **kwargs):
        payload = FeeScheduleCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        schedule = services.create_fee_schedule(**payload.validated_data)
        return Response(FeeScheduleSerializer(schedule).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="new-version")
    def new_version(self, request, pk=None):
        base = self.get_object()
        payload = FeeScheduleNewVersionSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        schedule = services.new_fee_schedule_version(base, **payload.validated_data)
        return Response(FeeScheduleSerializer(schedule).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["get"], pagination_class=None)
    def history(self, request, pk=None):
        chain = services.chain_of(self.get_object()).select_related("superseded_by")
        return Response(FeeScheduleSerializer(chain, many=True).data)


# --- invoices --------------------------------------------------------------------------------


@extend_schema_view(
    list=extend_schema(
        summary="List invoices",
        tags=["billing"],
        responses={200: InvoiceSerializer(many=True), **INVOICE_LIST_ERRORS},
    ),
    retrieve=extend_schema(
        summary="Get an invoice with its lines and open amounts",
        tags=["billing"],
        responses={
            200: InvoiceDetailSerializer,
            **error_responses(401, 403, 404, examples={404: INVOICE_404}),
        },
    ),
    generate=extend_schema(
        summary="Generate the invoice for an enrollment",
        description=(
            "Prices the enrollment from the fee schedule versions in force on the term's "
            "invoice_date (BR-FEE-01). Required: TUITION and REGISTRATION; LAB when the "
            "program has a lab; INTERNATIONAL_SERVICE for international students (422 "
            "FEE_SCHEDULE_NOT_FOUND otherwise). TECHNOLOGY, LIBRARY and STUDENT_ACTIVITY are "
            "charged when a version exists. Only ENROLLED enrollments (409 "
            "ILLEGAL_TRANSITION); one non-VOID invoice per enrollment (409 DUPLICATE_INVOICE). "
            "Waivers: waivable fee types only, 0 ≤ amount ≤ gross, reason when > 0 (BR-WAV-01)."
        ),
        tags=["billing"],
        request=InvoiceGenerateSerializer,
        responses={
            201: InvoiceDetailSerializer,
            **error_responses(
                400,
                401,
                403,
                409,
                422,
                examples={400: GENERATE_400, 409: GENERATE_409, 422: GENERATE_422},
            ),
        },
        examples=[
            OpenApiExample(
                "With a hardship waiver",
                request_only=True,
                value={
                    "enrollment_id": "ENR-2026-000001",
                    "waivers": [
                        {"fee_type": "TECHNOLOGY", "amount": "1500.00", "reason": "HARDSHIP"}
                    ],
                },
            )
        ],
    ),
    void=extend_schema(
        summary="Void an invoice",
        description="ISSUED only, and only when no scholarship and no payment is currently "
        "applied (BR-INV-08). Posts a VOID adjustment of minus each line's remaining net; "
        "the collection status becomes ADJUSTED_TO_ZERO. void_date is required (no default).",
        tags=["billing"],
        request=InvoiceVoidSerializer,
        responses={
            200: InvoiceDetailSerializer,
            **error_responses(
                400, 401, 403, 404, 409, examples={400: VOID_400, 404: INVOICE_404, 409: VOID_409}
            ),
        },
        examples=[OpenApiExample("Void", request_only=True, value={"void_date": "2026-08-20"})],
    ),
)
class InvoiceViewSet(
    _BillingPermissions, mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet
):
    queryset = Invoice.objects.prefetch_related("lines")
    serializer_class = InvoiceSerializer
    filterset_class = InvoiceFilter
    filter_backends = [DjangoFilterBackend, OrderingFilter]
    ordering_fields = ["invoice_id", "issue_date", "due_date"]
    ordering = ["invoice_id"]

    def get_serializer_class(self):
        return InvoiceDetailSerializer if self.action == "retrieve" else InvoiceSerializer

    @action(detail=False, methods=["post"])
    def generate(self, request):
        payload = InvoiceGenerateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        invoice = services.generate_invoice(
            enrollment=data["enrollment_id"], waivers=data["waivers"], actor=_actor(request)
        )
        invoice = Invoice.objects.prefetch_related("lines").get(pk=invoice.pk)
        return Response(InvoiceDetailSerializer(invoice).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def void(self, request, pk=None):
        invoice = self.get_object()
        payload = InvoiceVoidSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        services.void_invoice(
            invoice, void_date=payload.validated_data["void_date"], actor=_actor(request)
        )
        invoice = Invoice.objects.prefetch_related("lines").get(pk=invoice.pk)
        return Response(InvoiceDetailSerializer(invoice).data)


# --- batch operations --------------------------------------------------------------------------


class _BatchView(APIView):
    permission_classes = [ReadWriteRoles]
    read_roles = BILLING_READ
    write_roles = BILLING_WRITE
    http_method_names = ["post", "options"]


class LateFeeAssessView(_BatchView):
    @extend_schema(
        operation_id="billing_late_fees_assess",
        summary="Assess late fees",
        description=(
            "BR-LATE-01: for every ISSUED invoice without a late fee whose grace period "
            "(due_date + term grace days) ended before as_of_date, post a LATE_FEE line dated "
            "the day after: 2% of what was outstanding at the end of the grace period, at "
            "least ₹500 and at most ₹5,000. Once per invoice. as_of_date is required (no "
            "default); dry_run reports without writing."
        ),
        tags=["billing"],
        request=LateFeeAssessSerializer,
        responses={
            200: LateFeeResultSerializer,
            **error_responses(400, 401, 403, examples={400: LATE_FEE_400}),
        },
        examples=[
            OpenApiExample(
                "Dry run", request_only=True, value={"as_of_date": "2026-09-29", "dry_run": True}
            )
        ],
    )
    def post(self, request):
        payload = LateFeeAssessSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        result = services.assess_late_fees(
            as_of=data["as_of_date"], dry_run=data["dry_run"], actor=_actor(request)
        )
        result["assessed"] = [vars(r) for r in result["assessed"]]
        return Response(LateFeeResultSerializer(result).data)


class CollectionRefreshView(_BatchView):
    @extend_schema(
        operation_id="billing_collection_status_refresh",
        summary="Refresh invoice collection statuses",
        description=(
            "BR-INV-07: re-derive BR-INV-06 at as_of_date for every non-VOID invoice (e.g. "
            "UNPAID → OVERDUE the day after the due date). Invoices already derived for a "
            "later date are skipped. as_of_date is required (no default)."
        ),
        tags=["billing"],
        request=CollectionRefreshSerializer,
        responses={
            200: CollectionRefreshResultSerializer,
            **error_responses(400, 401, 403, examples={400: REFRESH_400}),
        },
        examples=[OpenApiExample("Refresh", request_only=True, value={"as_of_date": "2026-09-15"})],
    )
    def post(self, request):
        payload = CollectionRefreshSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        result = services.refresh_collection_status(
            as_of=payload.validated_data["as_of_date"], actor=_actor(request)
        )
        return Response(CollectionRefreshResultSerializer(result).data)


# --- student account views (D7) -------------------------------------------------------------------


@extend_schema_view(
    ledger=extend_schema(
        summary="A student's ledger entries",
        description="Entries with their source reference, by entry_date then entry_id. "
        "Entries post from invoice lines (CHARGE, LATE_FEE, ADJUSTMENT), scholarship "
        "allocations (SCHOLARSHIP_CREDIT, SCHOLARSHIP_REVERSAL), payments (PAYMENT) and "
        "processed refunds (REFUND). Payment allocations post nothing.",
        tags=["billing"],
        responses={
            200: LedgerEntrySerializer(many=True),
            **error_responses(401, 403, 404, examples={404: [STUDENT_404, INVALID_PAGE]}),
        },
    ),
    statement=extend_schema(
        summary="A student's statement",
        description="Invoices → lines with open amounts, scholarship and payment allocations "
        "(including deallocations), and the student's refunds.",
        tags=["billing"],
        responses={
            200: StatementSerializer,
            **error_responses(401, 403, 404, examples={404: STUDENT_404}),
        },
    ),
    balance=extend_schema(
        summary="A student's ledger balance",
        description="BR-BAL-01 ledger balance (entries dated on or before as_of, or all), its "
        "breakdown (signed like the ledger) and the BR-BAL-02 identity check: ledger balance "
        "= Σ invoice outstanding − unallocated credit, where unallocated credit is computed "
        "from the payment records (payments, net allocations, PROCESSED refunds).",
        tags=["billing"],
        parameters=[OpenApiParameter("as_of", type=str, required=False, description="YYYY-MM-DD")],
        responses={
            200: BalanceSerializer,
            **error_responses(400, 401, 403, 404, examples={400: BALANCE_400, 404: STUDENT_404}),
        },
    ),
)
class StudentAccountViewSet(_BillingPermissions, viewsets.GenericViewSet):
    queryset = Student.objects.all()
    serializer_class = LedgerEntrySerializer
    http_method_names = ["get", "head", "options"]
    filter_backends: list = []

    def _student(self, pk):
        return get_object_or_404(Student, pk=pk)

    @action(detail=True, methods=["get"])
    def ledger(self, request, pk=None):
        student = self._student(pk)
        entries = (
            LedgerEntry.objects.filter(student=student)
            .select_related(
                "invoice_line", "scholarship_allocation__invoice_line", "payment", "refund"
            )
            .order_by("entry_date", "entry_id")
        )
        page = self.paginate_queryset(entries)
        return self.get_paginated_response(LedgerEntrySerializer(page, many=True).data)

    @action(detail=True, methods=["get"], pagination_class=None)
    def statement(self, request, pk=None):
        student = self._student(pk)
        invoices = student.invoices.prefetch_related("lines").order_by("invoice_id")
        data = {
            "student_id": student.student_id,
            "invoices": list(invoices),
            "refunds": services.student_refunds(student),
        }
        return Response(StatementSerializer(data).data)

    @action(detail=True, methods=["get"], pagination_class=None)
    def balance(self, request, pk=None):
        student = self._student(pk)
        query = BalanceQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        as_of = query.validated_data.get("as_of")
        if as_of is not None:
            services.check_business_date("as_of", as_of)
        return Response(BalanceSerializer(services.student_balance(student, as_of)).data)
