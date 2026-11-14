"""Datagen API shapes (ARCHITECTURE §9.2 "Data generation and evaluation (admin only)").

Generation runs expose seed, scale, config hash, row counts and checksums, never labels. The
evaluation response is the report of ``datagen.evaluation``.
"""

from rest_framework import serializers

from datagen.config import DEFAULT_SEED
from datagen.models import GenerationRun


class GenerationRunSerializer(serializers.ModelSerializer):
    class Meta:
        model = GenerationRun
        fields = [
            "run_id",
            "seed",
            "scale_name",
            "scale_factor",
            "config_hash",
            "row_counts",
            "checksums",
            "code_version",
            "started_at",
            "finished_at",
        ]
        read_only_fields = fields


class GenerationRequestSerializer(serializers.Serializer):
    """POST /datagen/runs/: synchronous starter-scale generation (full scale: the command)."""

    seed = serializers.IntegerField(required=False, default=DEFAULT_SEED, min_value=0)
    scale = serializers.ChoiceField(choices=["starter"], required=False, default="starter")

    def validate(self, attrs):
        unknown = sorted(set(self.initial_data) - set(self.fields))
        if unknown:
            raise serializers.ValidationError({f: ["Unknown field."] for f in unknown})
        return attrs


class GenerationResultSerializer(serializers.Serializer):
    generation_run = GenerationRunSerializer()
    reconciliation_run = serializers.CharField(allow_null=True)
    evaluation = serializers.JSONField(allow_null=True)


class MetricsSerializer(serializers.Serializer):
    labels = serializers.IntegerField()
    findings = serializers.IntegerField()
    tp = serializers.IntegerField()
    fp = serializers.IntegerField()
    fn = serializers.IntegerField()
    precision = serializers.CharField(allow_null=True)
    recall = serializers.CharField(allow_null=True)
    f1 = serializers.CharField(allow_null=True)


class EvaluationSerializer(serializers.Serializer):
    generation_run = serializers.CharField()
    reconciliation_run = serializers.CharField()
    as_of_date = serializers.DateField()
    seed = serializers.IntegerField()
    scale_name = serializers.CharField()
    config_hash = serializers.CharField()
    labels_checksum = serializers.CharField()
    by_type = serializers.DictField(child=serializers.JSONField())
    overall = MetricsSerializer()
    consequential = serializers.IntegerField()
    consequential_findings = serializers.ListField(child=serializers.JSONField())
    unexpected = serializers.IntegerField()
    unexpected_by_type = serializers.DictField(child=serializers.IntegerField())
    unexpected_findings = serializers.ListField(child=serializers.JSONField())
    false_negatives = serializers.ListField(child=serializers.CharField())
    students = serializers.JSONField()
    targets = serializers.JSONField()
    meets_targets = serializers.BooleanField()
