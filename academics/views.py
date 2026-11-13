"""Academics API: /programs/, /terms/, /enrollments/ (ARCHITECTURE §9.2).

Withdraw and cancel actions arrive in Phase 5 (they post billing adjustments).
"""

from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import (
    OpenApiExample,
    extend_schema,
    extend_schema_view,
)
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.filters import OrderingFilter
from rest_framework.response import Response

from academics import services
from academics.filters import EnrollmentFilter, ProgramFilter, TermFilter
from academics.models import Enrollment, Program, Term
from academics.serializers import (
    EnrollmentCreateSerializer,
    EnrollmentEventSerializer,
    EnrollmentSerializer,
    ProgramCreateSerializer,
    ProgramSerializer,
    TermCreateSerializer,
    TermSerializer,
)
from core.dates import business_today
from core.permissions import ACADEMIC_WRITE, MASTER_DATA_READ, STAFF_READ, ReadWriteRoles
from core.schema import err, error_responses, invalid_choice, list_error_responses, not_found

ID_LOOKUP = r"[^/]+"  # any path segment: malformed IDs reach DRF → enveloped 404
# --- error examples: each is exactly what its endpoint returns (replayed by
# tests/verification/test_verify_openapi_examples.py). No shared defaults per resource.
PROGRAM_LIST_ERRORS = list_error_responses(invalid_choice("level", "PHD"))
PROGRAM_DETAIL_ERRORS = error_responses(401, 403, 404, examples={404: not_found("Program")})
PROGRAM_CREATE_400 = (
    "Credit range out of bounds",
    err(
        "VALIDATION_ERROR",
        "Request validation failed.",
        {"max_credits": ["Credits must satisfy 6 ≤ min_credits ≤ max_credits ≤ 24."]},
    ),
)

TERM_LIST_ERRORS = list_error_responses(invalid_choice("season", "WINTER"))
TERM_DETAIL_ERRORS = error_responses(401, 403, 404, examples={404: not_found("Term")})
TERM_CREATE_400 = (
    "Census date not start + 28",
    err(
        "VALIDATION_ERROR",
        "Request validation failed.",
        {"census_date": ["census_date must be start_date + 28 days (2027-02-22)."]},
    ),
)

ENROLLMENT_LIST_ERRORS = list_error_responses(invalid_choice("status", "PAUSED"))
ENROLLMENT_DETAIL_ERRORS = error_responses(401, 403, 404, examples={404: not_found("Enrollment")})
ENROLLMENT_CREATE_400 = (
    "After the add/drop window",
    err(
        "VALIDATION_ERROR",
        "Request validation failed.",
        {"enrolled_on": ["enrolled_on must be on or before 2026-09-14 (term start + 7 days)."]},
    ),
)
ENROLLMENT_CREATE_409 = (
    "Already enrolled",
    err(
        "ENROLLMENT_EXISTS",
        "STU-000001 is already enrolled in TERM-2026-FALL.",
        {
            "student_id": "STU-000001",
            "term_id": "TERM-2026-FALL",
            "existing_enrollment_id": "ENR-2026-000001",
        },
    ),
)
ENROLLMENT_CREATE_422 = [
    (
        "Credits outside the program range",
        err(
            "CREDITS_OUT_OF_RANGE",
            "Credits must be between 12 and 20 for PRG-001.",
            {"credits": 21, "min_credits": 12, "max_credits": 20, "program_id": "PRG-001"},
        ),
    ),
    (
        "Term not billable",
        err(
            "TERM_NOT_BILLABLE",
            "TERM-2025-FALL is not a billable term.",
            {"term_id": "TERM-2025-FALL"},
        ),
    ),
]


class _ReadCreateViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    permission_classes = [ReadWriteRoles]
    write_roles = ACADEMIC_WRITE
    filter_backends = [DjangoFilterBackend, OrderingFilter]
    lookup_value_regex = ID_LOOKUP
    http_method_names = ["get", "post", "head", "options"]


@extend_schema_view(
    list=extend_schema(
        summary="List programs",
        tags=["academics"],
        responses={200: ProgramSerializer(many=True), **PROGRAM_LIST_ERRORS},
    ),
    retrieve=extend_schema(
        summary="Get a program",
        tags=["academics"],
        responses={200: ProgramSerializer, **PROGRAM_DETAIL_ERRORS},
    ),
    create=extend_schema(
        summary="Create a program",
        tags=["academics"],
        request=ProgramCreateSerializer,
        responses={
            201: ProgramSerializer,
            **error_responses(400, 401, 403, examples={400: PROGRAM_CREATE_400}),
        },
        examples=[
            OpenApiExample(
                "New program",
                request_only=True,
                value={
                    "code": "BSC-CS",
                    "name": "BSc Computer Science",
                    "level": "UG",
                    "department": "Computing",
                    "is_stem": True,
                    "has_lab": True,
                    "min_credits": 12,
                    "max_credits": 20,
                    "is_active": True,
                },
            )
        ],
    ),
)
class ProgramViewSet(_ReadCreateViewSet):
    queryset = Program.objects.all()
    serializer_class = ProgramSerializer
    filterset_class = ProgramFilter
    read_roles = MASTER_DATA_READ
    ordering_fields = ["program_id", "code", "name"]
    ordering = ["program_id"]

    def create(self, request, *args, **kwargs):
        payload = ProgramCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        program = services.create_program(**payload.validated_data)
        return Response(ProgramSerializer(program).data, status=status.HTTP_201_CREATED)


@extend_schema_view(
    list=extend_schema(
        summary="List terms",
        tags=["academics"],
        responses={200: TermSerializer(many=True), **TERM_LIST_ERRORS},
    ),
    retrieve=extend_schema(
        summary="Get a term",
        tags=["academics"],
        responses={200: TermSerializer, **TERM_DETAIL_ERRORS},
    ),
    create=extend_schema(
        summary="Create a term",
        description="term_id is derived as TERM-{year}-{season}. census_date must be "
        "start_date + 28 days.",
        tags=["academics"],
        request=TermCreateSerializer,
        responses={
            201: TermSerializer,
            **error_responses(400, 401, 403, examples={400: TERM_CREATE_400}),
        },
        examples=[
            OpenApiExample(
                "TERM-2026-FALL",
                request_only=True,
                value={
                    "year": 2026,
                    "season": "FALL",
                    "is_billable": True,
                    "invoice_date": "2026-08-17",
                    "due_date": "2026-09-14",
                    "start_date": "2026-09-07",
                    "census_date": "2026-10-05",
                    "end_date": "2026-12-19",
                    "late_fee_grace_days": 14,
                },
            )
        ],
    ),
)
class TermViewSet(_ReadCreateViewSet):
    queryset = Term.objects.all()
    serializer_class = TermSerializer
    filterset_class = TermFilter
    read_roles = MASTER_DATA_READ
    ordering_fields = ["start_date", "term_id", "year"]
    ordering = ["start_date"]

    def create(self, request, *args, **kwargs):
        payload = TermCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        term = services.create_term(**payload.validated_data)
        return Response(TermSerializer(term).data, status=status.HTTP_201_CREATED)


@extend_schema_view(
    list=extend_schema(
        summary="List enrollments",
        tags=["academics"],
        responses={200: EnrollmentSerializer(many=True), **ENROLLMENT_LIST_ERRORS},
    ),
    retrieve=extend_schema(
        summary="Get an enrollment",
        tags=["academics"],
        responses={200: EnrollmentSerializer, **ENROLLMENT_DETAIL_ERRORS},
    ),
    create=extend_schema(
        summary="Enroll a student in a term",
        description=(
            "Validates: billable term (422 TERM_NOT_BILLABLE), credits within the program "
            "range (422 CREDITS_OUT_OF_RANGE), one enrollment per student and term "
            "(409 ENROLLMENT_EXISTS), enrolled_on ≤ term start + 7, admit term not after "
            "the enrolled term, active program (400). The program is the student's program."
        ),
        tags=["academics"],
        request=EnrollmentCreateSerializer,
        responses={
            201: EnrollmentSerializer,
            **error_responses(
                400,
                401,
                403,
                409,
                422,
                examples={
                    400: ENROLLMENT_CREATE_400,
                    409: ENROLLMENT_CREATE_409,
                    422: ENROLLMENT_CREATE_422,
                },
            ),
        },
        examples=[
            OpenApiExample(
                "Enroll",
                request_only=True,
                value={
                    "student": "STU-000001",
                    "term": "TERM-2026-FALL",
                    "credits": 16,
                    "enrolled_on": "2026-08-20",
                },
            )
        ],
    ),
    events=extend_schema(
        summary="Enrollment lifecycle history",
        tags=["academics"],
        responses={200: EnrollmentEventSerializer(many=True), **ENROLLMENT_DETAIL_ERRORS},
    ),
)
class EnrollmentViewSet(_ReadCreateViewSet):
    queryset = Enrollment.objects.select_related("student", "program", "term")
    serializer_class = EnrollmentSerializer
    filterset_class = EnrollmentFilter
    read_roles = STAFF_READ
    ordering_fields = ["enrollment_id", "enrolled_on"]
    ordering = ["enrollment_id"]

    def create(self, request, *args, **kwargs):
        payload = EnrollmentCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        enrollment = services.enroll(
            student=data["student"],
            term=data["term"],
            credits=data["credits"],
            enrolled_on=data.get("enrolled_on") or business_today(),
            actor=request.user.get_username(),
        )
        return Response(EnrollmentSerializer(enrollment).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["get"], pagination_class=None)
    def events(self, request, pk=None):
        enrollment = self.get_object()
        return Response(EnrollmentEventSerializer(enrollment.events.all(), many=True).data)
