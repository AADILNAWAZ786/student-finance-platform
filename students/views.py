"""Students API: /students/, /students/{id}/, /students/{id}/status/ (ARCHITECTURE §9.2).

Balance, ledger, statement and refundable views arrive with billing and payments.
"""

from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import OpenApiExample, extend_schema, extend_schema_view
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.filters import OrderingFilter
from rest_framework.response import Response

from core.dates import business_today
from core.permissions import ACADEMIC_WRITE, STAFF_READ, ReadWriteRoles
from core.schema import err, error_responses, invalid_choice, list_error_responses, not_found
from students import services
from students.filters import StudentFilter
from students.models import Student
from students.serializers import (
    StudentContactUpdateSerializer,
    StudentCreateSerializer,
    StudentSerializer,
    StudentStatusChangeResponseSerializer,
    StudentStatusChangeSerializer,
)

# --- error examples: each is exactly what this endpoint returns (replayed by
# tests/verification/test_verify_openapi_examples.py) ---------------------------------
STUDENT_404 = not_found("Student")
CREATE_400 = (
    "gpa sent as a JSON number",
    err(
        "VALIDATION_ERROR",
        "Request validation failed.",
        {"gpa": ['Send this decimal as a string, e.g. "3.70".']},
    ),
)
PATCH_400 = (
    "Non-contact field",
    err(
        "VALIDATION_ERROR",
        "Request validation failed.",
        {"gpa": ["Only contact fields (first_name, last_name, email) can be changed."]},
    ),
)
STATUS_400 = (
    "Unknown status",
    err(
        "VALIDATION_ERROR",
        "Request validation failed.",
        {"to_status": ['"SUSPENDED" is not a valid choice.']},
    ),
)
STATUS_409 = (
    "Illegal transition",
    err(
        "ILLEGAL_TRANSITION",
        "Student cannot move from GRADUATED to ACTIVE.",
        {
            "entity": "Student",
            "record_id": "STU-000001",
            "from_status": "GRADUATED",
            "to_status": "ACTIVE",
            "allowed": [],
        },
    ),
)

STUDENT_EXAMPLE = {
    "student_id": "STU-000001",
    "first_name": "Asha",
    "last_name": "Rao",
    "email": "stu000001@example.edu",
    "date_of_birth": "2006-03-14",
    "residency": "DOMESTIC",
    "program": "PRG-001",
    "admit_term": "TERM-2026-SPRING",
    "gpa": "3.72",
    "need_index": "0.35",
    "status": "ACTIVE",
}


@extend_schema_view(
    list=extend_schema(
        summary="List students",
        tags=["students"],
        responses={
            200: StudentSerializer(many=True),
            **list_error_responses(invalid_choice("status", "SUSPENDED")),
        },
    ),
    retrieve=extend_schema(
        summary="Get a student",
        tags=["students"],
        responses={
            200: StudentSerializer,
            **error_responses(401, 403, 404, examples={404: STUDENT_404}),
        },
    ),
    create=extend_schema(
        summary="Create a student",
        description="Assigns the next STU-###### ID, status ACTIVE, and writes the first "
        "status event. gpa and need_index are decimal strings.",
        tags=["students"],
        request=StudentCreateSerializer,
        responses={
            201: StudentSerializer,
            **error_responses(400, 401, 403, examples={400: CREATE_400}),
        },
        examples=[
            OpenApiExample(
                "New student",
                request_only=True,
                value={
                    "first_name": "Asha",
                    "last_name": "Rao",
                    "email": "stu000001@example.edu",
                    "date_of_birth": "2006-03-14",
                    "residency": "DOMESTIC",
                    "program": "PRG-001",
                    "admit_term": "TERM-2026-SPRING",
                    "gpa": "3.72",
                    "need_index": "0.35",
                },
            ),
            OpenApiExample(
                "Created", response_only=True, status_codes=["201"], value=STUDENT_EXAMPLE
            ),
        ],
    ),
    partial_update=extend_schema(
        summary="Update contact fields",
        description="Only first_name, last_name and email can change; any other field → 400.",
        tags=["students"],
        request=StudentContactUpdateSerializer,
        responses={
            200: StudentSerializer,
            **error_responses(400, 401, 403, 404, examples={400: PATCH_400, 404: STUDENT_404}),
        },
        examples=[
            OpenApiExample(
                "Change email", request_only=True, value={"email": "asha.rao@example.edu"}
            )
        ],
    ),
    change_status=extend_schema(
        summary="Change student status",
        description="Allowed: ACTIVE→ON_HOLD, ON_HOLD→ACTIVE, ACTIVE→WITHDRAWN, "
        "ACTIVE→GRADUATED. Anything else → 409 ILLEGAL_TRANSITION. Writes a "
        "StudentStatusEvent.",
        tags=["students"],
        request=StudentStatusChangeSerializer,
        responses={
            200: StudentStatusChangeResponseSerializer,
            **error_responses(
                400,
                401,
                403,
                404,
                409,
                examples={400: STATUS_400, 404: STUDENT_404, 409: STATUS_409},
            ),
        },
        examples=[
            OpenApiExample(
                "Financial hold",
                request_only=True,
                value={
                    "to_status": "ON_HOLD",
                    "reason": "FINANCIAL_HOLD",
                    "event_date": "2026-09-20",
                },
            )
        ],
    ),
)
class StudentViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    queryset = Student.objects.select_related("program", "admit_term")
    serializer_class = StudentSerializer
    filterset_class = StudentFilter
    filter_backends = [DjangoFilterBackend, OrderingFilter]
    ordering_fields = ["student_id", "last_name"]
    ordering = ["student_id"]
    permission_classes = [ReadWriteRoles]
    read_roles = STAFF_READ
    write_roles = ACADEMIC_WRITE
    lookup_value_regex = r"[^/]+"  # any segment; bad IDs → enveloped 404
    http_method_names = ["get", "post", "patch", "head", "options"]  # no PUT, no DELETE

    def create(self, request, *args, **kwargs):
        payload = StudentCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        student = services.create_student(
            **payload.validated_data, event_date=business_today(), actor=request.user.get_username()
        )
        return Response(StudentSerializer(student).data, status=status.HTTP_201_CREATED)

    def partial_update(self, request, *args, **kwargs):
        student = self.get_object()
        payload = StudentContactUpdateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        student = services.update_contact(student, **payload.validated_data)
        return Response(StudentSerializer(student).data)

    @action(detail=True, methods=["post"], url_path="status", url_name="status")
    def change_status(self, request, pk=None):
        student = self.get_object()
        payload = StudentStatusChangeSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        student, event = services.change_status(
            student,
            to_status=data["to_status"],
            reason=data["reason"],
            event_date=data.get("event_date") or business_today(),
            actor=request.user.get_username(),
        )
        return Response(
            StudentStatusChangeResponseSerializer({"student": student, "event": event}).data
        )
