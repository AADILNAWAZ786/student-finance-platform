"""Datagen API (ARCHITECTURE §9.2 "Data generation and evaluation (admin only)", §13).

Only platform_admin may read or write (superusers pass). Generation through the API is the
starter scale only and synchronous; full scale is the ``generate_data`` command. Runs are read
+ action only: no PUT / PATCH / DELETE. The evaluation is computed on demand by
``datagen.evaluation`` (the only code that reads both findings and labels) and stored nowhere.
"""

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiParameter,
    extend_schema,
    extend_schema_view,
)
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.filters import OrderingFilter
from rest_framework.response import Response

from core.permissions import PLATFORM_ADMIN, ReadWriteRoles
from core.schema import error_responses
from datagen import openapi as ex
from datagen import services
from datagen.evaluation import EvaluationError, evaluate
from datagen.models import GenerationRun
from datagen.serializers import (
    EvaluationSerializer,
    GenerationRequestSerializer,
    GenerationResultSerializer,
    GenerationRunSerializer,
)


@extend_schema_view(
    list=extend_schema(
        summary="List generation runs",
        tags=["datagen"],
        responses={200: GenerationRunSerializer(many=True), **ex.RUN_LIST_ERRORS},
    ),
    retrieve=extend_schema(
        summary="Get a generation run: seed, scale, config hash, row counts, checksums",
        tags=["datagen"],
        responses={
            200: GenerationRunSerializer,
            **error_responses(401, 403, 404, examples={404: ex.RUN_404}),
        },
    ),
    create=extend_schema(
        summary="Generate the starter-scale dataset",
        description=(
            "Synchronous starter-scale generation (ARCHITECTURE §11.2 stages 1–19): baseline, "
            "injected anomalies with ground-truth labels in the separate ground-truth database, "
            "the blind reconciliation run (triggered_by DATAGEN) and its evaluation. Refused "
            "with 409 DATASET_NOT_FRESH unless both databases are fresh (no force option). "
            "Full scale: the generate_data command. Labels are never returned."
        ),
        tags=["datagen"],
        request=GenerationRequestSerializer,
        responses={
            201: GenerationResultSerializer,
            **error_responses(
                400,
                401,
                403,
                409,
                examples={400: ex.RUN_CREATE_400, 409: ex.RUN_CREATE_409},
            ),
        },
        examples=[
            OpenApiExample(
                "Starter scale, default seed", request_only=True, value={"scale": "starter"}
            ),
            OpenApiExample(
                "Starter scale, another seed",
                request_only=True,
                value={"scale": "starter", "seed": 7},
            ),
        ],
    ),
)
class GenerationRunViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    permission_classes = [ReadWriteRoles]
    read_roles = write_roles = (PLATFORM_ADMIN,)
    lookup_value_regex = r"[^/]+"
    http_method_names = ["get", "post", "head", "options"]
    filter_backends = [OrderingFilter]
    queryset = GenerationRun.objects.all()
    serializer_class = GenerationRunSerializer
    ordering_fields = ["run_id"]
    ordering = ["run_id"]

    def create(self, request, *args, **kwargs):
        payload = GenerationRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        result = services.generate(seed=data["seed"], scale=data["scale"])
        body = GenerationResultSerializer(
            {
                "generation_run": result.run,
                "reconciliation_run": result.reconciliation_run.pk
                if result.reconciliation_run
                else None,
                "evaluation": result.evaluation,
            }
        ).data
        return Response(body, status=status.HTTP_201_CREATED)

    @extend_schema(
        summary="Evaluate a reconciliation run against the ground truth",
        description=(
            "Precision, recall and F1 per required issue type (ARCHITECTURE §12.3): findings and "
            "labels joined on (issue_type, record_id); a finding of another type on a record in "
            "a label's related_ids is consequential, not a false positive; every other finding "
            "is unexpected. reconciliation_run must be a COMPLETED, ALL-scope run. Computed on "
            "demand; nothing is stored."
        ),
        tags=["datagen"],
        parameters=[
            OpenApiParameter(
                "reconciliation_run",
                OpenApiTypes.STR,
                required=True,
                description="RECON-… (COMPLETED, scope ALL)",
            )
        ],
        responses={
            200: EvaluationSerializer,
            **error_responses(
                400, 401, 403, 404, examples={400: ex.EVALUATION_400, 404: ex.RUN_404}
            ),
        },
    )
    @action(detail=True, methods=["get"], url_path="evaluation")
    def evaluation(self, request, pk=None):
        run = self.get_object()
        recon = request.query_params.get("reconciliation_run")
        if not recon:
            raise serializers.ValidationError(
                {"reconciliation_run": ["This query parameter is required."]}
            )
        try:
            report = evaluate(generation_run_id=run.pk, reconciliation_run_id=recon)
        except EvaluationError as exc:
            raise serializers.ValidationError({"reconciliation_run": [str(exc)]}) from exc
        return Response(EvaluationSerializer(report).data)
