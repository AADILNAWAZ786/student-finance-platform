"""Reconciliation API (ARCHITECTURE §9.2, Reconciliation; decision D-R11 adds the read-only
snapshot list/detail and finding detail). Records are read + action only: no PUT / PATCH /
DELETE (§9.1).

Permissions (ARCHITECTURE §13): platform_admin takes snapshots and runs reconciliation;
platform_admin and support_staff ("read everything") read runs, findings and snapshots;
everyone else, learners included, has no access.
"""

from django.db.models import Count
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import OpenApiExample, extend_schema, extend_schema_view
from rest_framework import mixins, status, viewsets
from rest_framework.filters import OrderingFilter
from rest_framework.response import Response

from core.permissions import BILLING_READ, BILLING_WRITE, ReadWriteRoles
from core.schema import error_responses
from reconciliation import openapi as ex
from reconciliation import policy, services
from reconciliation.filters import (
    AnomalyFindingFilter,
    BalanceSnapshotFilter,
    ReconciliationRunFilter,
)
from reconciliation.models import AnomalyFinding, BalanceSnapshot, ReconciliationRun
from reconciliation.serializers import (
    AnomalyFindingSerializer,
    BalanceSnapshotSerializer,
    ReconciliationRunSerializer,
    RunRequestSerializer,
    SnapshotRequestSerializer,
    SnapshotResponseSerializer,
)

ID_LOOKUP = r"[^/]+"


class _Permissions:
    permission_classes = [ReadWriteRoles]
    read_roles = BILLING_READ  # platform_admin, support_staff
    write_roles = BILLING_WRITE  # platform_admin
    lookup_value_regex = ID_LOOKUP
    http_method_names = ["get", "post", "head", "options"]
    filter_backends = [DjangoFilterBackend, OrderingFilter]


def _runs():
    return ReconciliationRun.objects.annotate(findings_count=Count("findings"))


@extend_schema_view(
    list=extend_schema(
        summary="List reconciliation runs",
        tags=["reconciliation"],
        responses={200: ReconciliationRunSerializer(many=True), **ex.RUN_LIST_ERRORS},
    ),
    retrieve=extend_schema(
        summary="Get a reconciliation run with its summary metrics",
        tags=["reconciliation"],
        responses={
            200: ReconciliationRunSerializer,
            **error_responses(401, 403, 404, examples={404: ex.RUN_404}),
        },
    ),
    create=extend_schema(
        summary="Run reconciliation",
        description=(
            "Runs every detector check (ARCHITECTURE §12.1: structural, relationship, temporal, "
            "lifecycle, arithmetic, the five required anomaly types, and the BR-BAL-02 identity "
            "per student) for every student (scope ALL) or one (scope STUDENT, student_id "
            "required) as of as_of_date. A STUDENT run skips the global checks and lists them "
            "in summary.skipped_checks. Synchronous. The run is stored RUNNING first; the "
            "checks read outside any transaction, so writes committed during the run may be "
            "seen part-way (not a snapshot: re-run once they finish); then every finding and "
            "the COMPLETED status are written in one transaction. If anything fails, the run "
            "is FAILED with no findings and summary.error. Domain data is never changed. "
            "Findings: GET /anomalies/?run=."
        ),
        tags=["reconciliation"],
        request=RunRequestSerializer,
        responses={
            201: ReconciliationRunSerializer,
            **error_responses(400, 401, 403, examples={400: ex.RUN_CREATE_400}),
        },
        examples=[
            OpenApiExample(
                "Every student",
                request_only=True,
                value={"scope": "ALL", "as_of_date": "2026-12-31"},
            ),
            OpenApiExample(
                "One student",
                request_only=True,
                value={"scope": "STUDENT", "student_id": "STU-000001", "as_of_date": "2026-12-31"},
            ),
        ],
    ),
)
class ReconciliationRunViewSet(
    _Permissions,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    queryset = _runs()
    serializer_class = ReconciliationRunSerializer
    filterset_class = ReconciliationRunFilter
    ordering_fields = ["run_id", "as_of_date"]
    ordering = ["run_id"]

    def create(self, request, *args, **kwargs):
        payload = RunRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        run = services.run_reconciliation(
            scope=data["scope"],
            student=data.get("student_id"),
            as_of_date=data["as_of_date"],
            triggered_by=policy.API,
        )
        body = ReconciliationRunSerializer(_runs().get(pk=run.pk)).data
        return Response(body, status=status.HTTP_201_CREATED)


@extend_schema_view(
    list=extend_schema(
        summary="List detected anomalies (findings)",
        description="Filters: run, issue_type, student, severity. Never an input to anything.",
        tags=["reconciliation"],
        responses={200: AnomalyFindingSerializer(many=True), **ex.FINDING_LIST_ERRORS},
    ),
    retrieve=extend_schema(
        summary="Get one finding",
        tags=["reconciliation"],
        responses={
            200: AnomalyFindingSerializer,
            **error_responses(401, 403, 404, examples={404: ex.FINDING_404}),
        },
    ),
)
class AnomalyFindingViewSet(
    _Permissions, mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet
):
    queryset = AnomalyFinding.objects.all()
    serializer_class = AnomalyFindingSerializer
    filterset_class = AnomalyFindingFilter
    ordering_fields = ["finding_id"]
    ordering = ["finding_id"]


@extend_schema_view(
    list=extend_schema(
        summary="List balance snapshots",
        tags=["reconciliation"],
        responses={200: BalanceSnapshotSerializer(many=True), **ex.SNAPSHOT_LIST_ERRORS},
    ),
    retrieve=extend_schema(
        summary="Get one balance snapshot",
        tags=["reconciliation"],
        responses={
            200: BalanceSnapshotSerializer,
            **error_responses(401, 403, 404, examples={404: ex.SNAPSHOT_404}),
        },
    ),
    create=extend_schema(
        summary="Take balance snapshots",
        description=(
            "Snapshots one student (student_id) or every student at as_of_date: "
            "reported_balance = the ledger balance (BR-BAL-01), total_debits / total_credits "
            "= Σ positive / Σ negative entries dated on or before it, in one transaction. A "
            "snapshot that already exists for the student and date is returned unchanged with "
            "created = false (201 when at least one snapshot was created, otherwise 200). "
            "Reconciliation checks each snapshot against the ledger (BR-BAL-03)."
        ),
        tags=["reconciliation"],
        request=SnapshotRequestSerializer,
        responses={
            201: SnapshotResponseSerializer,
            200: SnapshotResponseSerializer,
            **error_responses(400, 401, 403, examples={400: ex.SNAPSHOT_CREATE_400}),
        },
        examples=[
            OpenApiExample(
                "One student",
                request_only=True,
                value={"student_id": "STU-000001", "as_of_date": "2026-12-19"},
            ),
            OpenApiExample("Every student", request_only=True, value={"as_of_date": "2026-12-31"}),
        ],
    ),
)
class BalanceSnapshotViewSet(
    _Permissions,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    queryset = BalanceSnapshot.objects.all()
    serializer_class = BalanceSnapshotSerializer
    filterset_class = BalanceSnapshotFilter
    ordering_fields = ["snapshot_id", "as_of_date"]
    ordering = ["snapshot_id"]

    def create(self, request, *args, **kwargs):
        payload = SnapshotRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        student = data.get("student_id")
        results = services.take_snapshot(as_of_date=data["as_of_date"], student=student)
        rows = []
        for r in results:
            r.snapshot.created = r.created
            rows.append(r.snapshot)
        created = sum(1 for r in results if r.created)
        body = SnapshotResponseSerializer(
            {
                "as_of_date": data["as_of_date"],
                "student_id": student.pk if student else None,
                "created_count": created,
                "existing_count": len(results) - created,
                "snapshots": rows,
            }
        ).data
        return Response(body, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)
