"""Data generation and evaluation models (DATABASE_DESIGN.md §3 datagen).

* ``GenerationRun`` lives in the **default** database: run metadata (seed, scale, config hash,
  row counts, content checksums). It holds nothing about injected anomalies.
* ``GroundTruthLabel`` lives **only** in the ``ground_truth`` database (ARCHITECTURE §12.2;
  ``config.routers.GroundTruthRouter``). Its links to other rows are plain IDs, never foreign
  keys: cross-database relations are refused by the router.

Label IDs (``GT-{year}-{6}``) are numbered in memory by the generator and never drawn from
``core.IdSequence``, which lives in the domain database the detector reads (decision D-G3).
"""

from django.db import models
from django.db.models import Q

from core.ids import ID_MAX_LENGTHS, id_validator

REQUIRED_ISSUE_TYPES = (
    "PAYMENT_OVER_ALLOCATED",
    "SCHOLARSHIP_EXCEEDS_ELIGIBLE_FEES",
    "DUPLICATE_INVOICE",
    "REFUND_EXCEEDS_NET_PAID",
    "BALANCE_LEDGER_MISMATCH",
)
INJECTION_METHODS = ("APPEND", "MODIFY")
DECISIONS = (
    "REJECT_ALLOCATION",
    "CAP_SCHOLARSHIP",
    "VOID_DUPLICATE",
    "REVERSE_REFUND",
    "RESTATE_BALANCE",
    "INVESTIGATE",
)
SCALE_NAMES = ("starter", "full", "custom")
MONEY = {"max_digits": 12, "decimal_places": 2}


class GenerationRun(models.Model):
    """One synthetic dataset generation (metadata; not a dataset table)."""

    run_id = models.CharField(
        max_length=ID_MAX_LENGTHS["generation_run"],
        primary_key=True,
        validators=[id_validator("generation_run")],
    )
    seed = models.BigIntegerField(default=20261022)
    scale_name = models.CharField(max_length=10, choices=[(s, s) for s in SCALE_NAMES])
    scale_factor = models.DecimalField(max_digits=5, decimal_places=4)
    config_hash = models.CharField(max_length=64)
    row_counts = models.JSONField()
    checksums = models.JSONField()
    code_version = models.CharField(max_length=40, null=True, blank=True)  # noqa: DJ001
    started_at = models.DateTimeField()  # execution metadata; not exported
    finished_at = models.DateTimeField(null=True, blank=True)  # execution metadata
    recorded_at = models.DateTimeField(auto_now_add=True)  # execution metadata; not exported

    class Meta:
        ordering = ("run_id",)
        constraints = [
            models.CheckConstraint(
                condition=Q(scale_name__in=list(SCALE_NAMES)), name="genrun_scale_name_enum"
            ),
            models.CheckConstraint(
                condition=Q(scale_factor__gt=0) & Q(scale_factor__lte=1),
                name="genrun_scale_factor_range",
            ),
        ]

    def __str__(self) -> str:
        return self.run_id


class GroundTruthLabel(models.Model):
    """One injected anomaly (ground_truth database only)."""

    label_id = models.CharField(
        max_length=ID_MAX_LENGTHS["ground_truth_label"],
        primary_key=True,
        validators=[id_validator("ground_truth_label")],
    )
    generation_run_id = models.CharField(
        max_length=ID_MAX_LENGTHS["generation_run"], validators=[id_validator("generation_run")]
    )
    issue_type = models.CharField(max_length=36, choices=[(t, t) for t in REQUIRED_ISSUE_TYPES])
    entity_type = models.CharField(max_length=24)
    record_id = models.CharField(max_length=64)
    student_id = models.CharField(
        max_length=ID_MAX_LENGTHS["student"], validators=[id_validator("student")]
    )
    related_ids = models.JSONField()
    rule_violated = models.CharField(max_length=12)
    injection_method = models.CharField(max_length=8, choices=[(m, m) for m in INJECTION_METHODS])
    expected_value = models.DecimalField(**MONEY, null=True, blank=True)
    injected_value = models.DecimalField(**MONEY, null=True, blank=True)
    expected_decision = models.CharField(max_length=24, choices=[(d, d) for d in DECISIONS])
    recorded_at = models.DateTimeField(auto_now_add=True)  # execution metadata; not exported

    class Meta:
        ordering = ("label_id",)
        constraints = [
            models.UniqueConstraint(
                fields=["generation_run_id", "issue_type", "record_id"],
                name="gtlabel_unique_run_type_record",
            ),
            models.CheckConstraint(
                condition=Q(issue_type__in=list(REQUIRED_ISSUE_TYPES)), name="gtlabel_issue_enum"
            ),
            models.CheckConstraint(
                condition=Q(injection_method__in=list(INJECTION_METHODS)),
                name="gtlabel_method_enum",
            ),
            models.CheckConstraint(
                condition=Q(expected_decision__in=list(DECISIONS)), name="gtlabel_decision_enum"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.label_id} {self.issue_type} {self.record_id}"
