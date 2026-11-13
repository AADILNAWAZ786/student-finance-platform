"""Scholarships API (ARCHITECTURE §9.2, Scholarships). Paths exactly as documented; no
type or eligibility detail routes, no revoke route (D-S14), no PUT/PATCH/DELETE.
Permissions follow the Phase 2 billing matrix (D6): write platform_admin; read
platform_admin and support_staff.
"""

from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import OpenApiExample, extend_schema, extend_schema_view
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.filters import OrderingFilter
from rest_framework.response import Response

from core.permissions import BILLING_READ, BILLING_WRITE, ReadWriteRoles
from core.schema import err, error_responses, invalid_choice, list_error_responses, not_found
from scholarships import services
from scholarships.filters import EligibilityFilter, ScholarshipFilter, ScholarshipTypeFilter
from scholarships.models import Eligibility, Scholarship, ScholarshipType
from scholarships.serializers import (
    ApplySerializer,
    AwardCreateSerializer,
    EligibilityCheckSerializer,
    EligibilitySerializer,
    ScholarshipDetailSerializer,
    ScholarshipSerializer,
    ScholarshipTypeCreateSerializer,
    ScholarshipTypeSerializer,
)

ID_LOOKUP = r"[^/]+"
INVALID = "Request validation failed."
INV = "INV-STU-000001-2026-000001"
EXCEEDS = "SCHOLARSHIP_EXCEEDS_ELIGIBLE_FEES"

# --- error examples (each replayed against the live API by
# tests/verification/test_verify_openapi_examples.py) ---------------------------------------
TYPE_LIST_ERRORS = list_error_responses(invalid_choice("category", "SPORTS"))
TYPE_CREATE_400 = [
    (
        "Program list missing for a restricted type",
        err(
            "VALIDATION_ERROR",
            INVALID,
            {"program_ids": ["Required (a non-empty list) when program_restricted is true."]},
        ),
    ),
    (
        "Second ALL_FEE_LINES type",
        err(
            "VALIDATION_ERROR",
            INVALID,
            {"coverage_scope": ["Only one ALL_FEE_LINES type may exist; it is SCT-007."]},
        ),
    ),
]
CHECK_400 = [
    (
        "Unknown student",
        err(
            "VALIDATION_ERROR",
            INVALID,
            {"student_id": ['Invalid pk "STU-999999" - object does not exist.']},
        ),
    ),
    ("evaluated_on sent", err("VALIDATION_ERROR", INVALID, {"evaluated_on": ["Unknown field."]})),
]
CHECK_422 = (
    "Term not billable",
    err(
        "TERM_NOT_BILLABLE",
        "TERM-2025-SPRING is not a billable term.",
        {"term_id": "TERM-2025-SPRING"},
    ),
)
ELIGIBILITY_LIST_ERRORS = list_error_responses(
    (
        "Invalid evaluated_on filter",
        err("VALIDATION_ERROR", INVALID, {"evaluated_on": ["Enter a valid date."]}),
    )
)
AWARD_400 = (
    "requested_amount not positive",
    err("VALIDATION_ERROR", INVALID, {"requested_amount": ["Must be greater than 0."]}),
)
AWARD_409 = (
    "Exclusive group conflict",
    err(
        "SCHOLARSHIP_GROUP_CONFLICT",
        "SCT-001 conflicts with existing award(s) SCH-2026-000001 for the same student and term.",
        {
            "scholarship_type_id": "SCT-001",
            "exclusive_group": "MERIT",
            "conflicting_scholarship_ids": ["SCH-2026-000001"],
        },
        rule_id="BR-SCH-05",
    ),
)
AWARD_422 = [
    (
        "Not a positive eligibility",
        err(
            "SCHOLARSHIP_NOT_ELIGIBLE",
            "ELG-2026-000003 is not a positive eligibility.",
            {"eligibility_id": "ELG-2026-000003", "reason_codes": ["GPA_AT_OR_ABOVE_MAX"]},
            rule_id="BR-SCH-02",
        ),
    ),
    (
        "Over the maximum award",
        err(
            EXCEEDS,
            "Requested 75000.01 exceeds the maximum award 75000.00.",
            {
                "scholarship_type_id": "SCT-001",
                "eligible_fee_base": "150000.00",
                "already_awarded_this_term": "0.00",
                "computed_award": "75000.00",
                "reserved_on_invoice": "0.00",
                "maximum_award": "75000.00",
                "requested_amount": "75000.01",
            },
            rule_id="BR-SCH-03",
        ),
    ),
]
SCHOLARSHIP_LIST_ERRORS = list_error_responses(invalid_choice("status", "PENDING"))
SCHOLARSHIP_404 = not_found("Scholarship")
APPLY_400 = (
    "allocated_on before the award",
    err(
        "VALIDATION_ERROR",
        INVALID,
        {
            "allocated_on": [
                "allocated_on must be on or after 2026-08-20, the award date or the invoice's "
                "latest posting or status date."
            ]
        },
    ),
)
APPLY_409 = (
    "Already applied",
    err(
        "ILLEGAL_TRANSITION",
        "Scholarship cannot move from APPLIED to APPLIED.",
        {
            "entity": "Scholarship",
            "record_id": "SCH-2026-000001",
            "from_status": "APPLIED",
            "to_status": "APPLIED",
            "allowed": ["PARTIALLY_REVERSED", "REVOKED"],
        },
    ),
)
APPLY_422 = (
    "Above the eligible fee base",
    err(
        EXCEEDS,
        f"Applying 200000.00 would bring scholarship applied to {INV} to 200000.00, above its "
        "eligible fee base 150000.00.",
        {
            "scholarship_id": "SCH-2026-000001",
            "invoice_id": INV,
            "eligible_fee_base": "150000.00",
            "already_applied": "0.00",
            "amount": "200000.00",
        },
        rule_id="BR-SCH-04",
    ),
)


def _actor(request) -> str:
    return request.user.get_username()


class _ScholarshipPermissions:
    permission_classes = [ReadWriteRoles]
    read_roles = BILLING_READ
    write_roles = BILLING_WRITE
    lookup_value_regex = ID_LOOKUP
    http_method_names = ["get", "post", "head", "options"]
    filter_backends = [DjangoFilterBackend, OrderingFilter]


@extend_schema_view(
    list=extend_schema(
        summary="List scholarship types (criteria)",
        tags=["scholarships"],
        responses={200: ScholarshipTypeSerializer(many=True), **TYPE_LIST_ERRORS},
    ),
    create=extend_schema(
        summary="Create a scholarship type",
        description="Types are immutable (a criteria change is a new type). program_ids is "
        "required (non-empty) when program_restricted is true. Only one ALL_FEE_LINES type may "
        "exist, with category FULL_RIDE, exclusive_group FULL_RIDE and priority 0.",
        tags=["scholarships"],
        request=ScholarshipTypeCreateSerializer,
        responses={
            201: ScholarshipTypeSerializer,
            **error_responses(400, 401, 403, examples={400: TYPE_CREATE_400}),
        },
        examples=[
            OpenApiExample(
                "STEM award",
                request_only=True,
                value={
                    "name": "STEM Award",
                    "category": "PROGRAM",
                    "min_gpa": "3.00",
                    "residency": "ALL",
                    "first_term_only": False,
                    "program_restricted": True,
                    "program_ids": ["PRG-001"],
                    "coverage_scope": "ELIGIBLE_LINES_ONLY",
                    "award_basis": "FIXED",
                    "award_value": "20000.00",
                    "max_amount_per_term": "20000.00",
                    "priority": 4,
                    "effective_from": "2026-01-01",
                },
            )
        ],
    ),
)
class ScholarshipTypeViewSet(
    _ScholarshipPermissions, mixins.ListModelMixin, mixins.CreateModelMixin, viewsets.GenericViewSet
):
    queryset = ScholarshipType.objects.prefetch_related("type_programs")
    serializer_class = ScholarshipTypeSerializer
    filterset_class = ScholarshipTypeFilter
    ordering_fields = ["priority", "scholarship_type_id"]
    ordering = ["priority", "scholarship_type_id"]

    def create(self, request, *args, **kwargs):
        payload = ScholarshipTypeCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        stype = services.create_scholarship_type(**payload.validated_data)
        stype = ScholarshipType.objects.prefetch_related("type_programs").get(pk=stype.pk)
        return Response(ScholarshipTypeSerializer(stype).data, status=status.HTTP_201_CREATED)


@extend_schema_view(
    list=extend_schema(
        summary="Eligibility evaluation history",
        description="Every evaluation is kept, eligible or not; the latest per (student, "
        "type, term) is current.",
        tags=["scholarships"],
        responses={200: EligibilitySerializer(many=True), **ELIGIBILITY_LIST_ERRORS},
    ),
)
class EligibilityViewSet(_ScholarshipPermissions, mixins.ListModelMixin, viewsets.GenericViewSet):
    queryset = Eligibility.objects.all()
    serializer_class = EligibilitySerializer
    filterset_class = EligibilityFilter
    ordering_fields = ["eligibility_id", "evaluated_on"]
    ordering = ["eligibility_id"]


@extend_schema_view(
    list=extend_schema(
        summary="List scholarship awards",
        tags=["scholarships"],
        responses={200: ScholarshipSerializer(many=True), **SCHOLARSHIP_LIST_ERRORS},
    ),
    retrieve=extend_schema(
        summary="Get an award with its allocations and events",
        tags=["scholarships"],
        responses={
            200: ScholarshipDetailSerializer,
            **error_responses(401, 403, 404, examples={404: SCHOLARSHIP_404}),
        },
    ),
    create=extend_schema(
        summary="Award a scholarship",
        description=(
            "From a positive, current, unused eligibility (BR-SCH-02) to the student's ISSUED "
            "invoice for the term. Amount: PERCENT of the eligible fee base or FIXED, capped "
            "by the per-term cap less what this type already awarded the student this term "
            "(BR-SCH-03), and by the invoice's eligible fees not yet reserved by other awards. "
            "requested_amount may only reduce it (422 above). Exclusive groups (BR-SCH-05) and "
            "full-ride exclusivity (BR-SCH-08): 409. award_date defaults to the later of "
            "evaluated_on and the invoice issue date."
        ),
        tags=["scholarships"],
        request=AwardCreateSerializer,
        responses={
            201: ScholarshipDetailSerializer,
            **error_responses(
                400, 401, 403, 409, 422, examples={400: AWARD_400, 409: AWARD_409, 422: AWARD_422}
            ),
        },
        examples=[
            OpenApiExample(
                "Merit award",
                request_only=True,
                value={"eligibility_id": "ELG-2026-000002", "invoice_id": INV},
            )
        ],
    ),
    eligibility_checks=extend_schema(
        summary="Evaluate a student against every scholarship type",
        description="Stores one Eligibility row per type, eligible or not, with reason codes "
        "and the input snapshot (BR-SCH-01). evaluated_on is always the term's invoice_date.",
        tags=["scholarships"],
        request=EligibilityCheckSerializer,
        responses={
            201: EligibilitySerializer(many=True),
            **error_responses(400, 401, 403, 422, examples={400: CHECK_400, 422: CHECK_422}),
        },
        examples=[
            OpenApiExample(
                "Check",
                request_only=True,
                value={"student_id": "STU-000001", "term_id": "TERM-2026-FALL"},
            )
        ],
    ),
    apply=extend_schema(
        summary="Apply an award to its invoice lines",
        description=(
            "All-or-nothing. Allocates the award to the type's coverable FEE lines in "
            "allocation-priority order (BR-SCH-06/08), within each line's remaining room, and "
            "keeps the scholarship applied to the invoice within its eligible fee base "
            "(BR-SCH-04). Posts SCHOLARSHIP_CREDIT ledger entries, moves the award OFFERED → "
            "ACCEPTED → APPLIED and refreshes the invoice's collection status. Scholarship "
            "takes precedence over payments (BR-ALC-06)."
        ),
        tags=["scholarships"],
        request=ApplySerializer,
        responses={
            200: ScholarshipDetailSerializer,
            **error_responses(
                400,
                401,
                403,
                404,
                409,
                422,
                examples={400: APPLY_400, 404: SCHOLARSHIP_404, 409: APPLY_409, 422: APPLY_422},
            ),
        },
        examples=[OpenApiExample("Apply", request_only=True, value={})],
    ),
)
class ScholarshipViewSet(
    _ScholarshipPermissions,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    queryset = Scholarship.objects.prefetch_related("allocations", "events")
    serializer_class = ScholarshipSerializer
    filterset_class = ScholarshipFilter
    ordering_fields = ["scholarship_id", "award_date"]
    ordering = ["scholarship_id"]

    def get_serializer_class(self):
        return ScholarshipDetailSerializer if self.action == "retrieve" else ScholarshipSerializer

    def _detail(self, pk):
        return ScholarshipDetailSerializer(
            Scholarship.objects.prefetch_related("allocations", "events").get(pk=pk)
        ).data

    def create(self, request, *args, **kwargs):
        payload = AwardCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        award = services.award_scholarship(
            eligibility=data["eligibility_id"],
            invoice=data["invoice_id"],
            requested_amount=data.get("requested_amount"),
            award_date=data.get("award_date"),
            actor=_actor(request),
        )
        return Response(self._detail(award.pk), status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["post"], url_path="eligibility-checks", pagination_class=None)
    def eligibility_checks(self, request):
        payload = EligibilityCheckSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        rows = services.evaluate_eligibility(
            student=payload.validated_data["student_id"], term=payload.validated_data["term_id"]
        )
        return Response(EligibilitySerializer(rows, many=True).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def apply(self, request, pk=None):
        award = self.get_object()
        payload = ApplySerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        services.apply_scholarship(
            award, allocated_on=payload.validated_data.get("allocated_on"), actor=_actor(request)
        )
        return Response(self._detail(award.pk))
