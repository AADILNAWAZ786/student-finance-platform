"""Reconciliation models (DATABASE_DESIGN.md §3 reconciliation; default database only).

* ``BalanceSnapshot``: the reported ("AR system") balance of one student at a date, checked
  against the ledger by BR-BAL-03. Append-only.
* ``ReconciliationRun``: one detector run. Only ``status``, ``finished_at`` and ``summary``
  change after insertion (RUNNING → COMPLETED / FAILED, decision D-R1).
* ``AnomalyFinding``: what a run found. Append-only; never an input to anything, and it has
  no reference to labels of any kind (ARCHITECTURE §12.2).

DB constraints are limited to enums, signs and required-iff rules (ARCHITECTURE §8.3).
``QuerySet.update()`` bypasses the model guards; no reconciliation code uses it.
"""

from django.db import models
from django.db.models import Q

from core.ids import ID_MAX_LENGTHS, id_validator
from reconciliation import policy

MONEY = {"max_digits": 12, "decimal_places": 2}


class AppendOnlyError(RuntimeError):
    """A reconciliation row was changed in a way its table does not allow."""


def _choices(values):
    return [(v, v) for v in values]


class BalanceSnapshot(models.Model):
    snapshot_id = models.CharField(
        max_length=ID_MAX_LENGTHS["balance_snapshot"],
        primary_key=True,
        validators=[id_validator("balance_snapshot")],
    )
    student = models.ForeignKey(
        "students.Student", on_delete=models.PROTECT, related_name="balance_snapshots"
    )
    as_of_date = models.DateField()
    reported_balance = models.DecimalField(**MONEY)
    total_debits = models.DecimalField(**MONEY, help_text="Σ positive entries ≤ as_of_date")
    total_credits = models.DecimalField(**MONEY, help_text="Σ negative entries ≤ as_of_date")
    recorded_at = models.DateTimeField(auto_now_add=True)  # execution metadata; not exported

    class Meta:
        ordering = ("snapshot_id",)
        constraints = [
            models.UniqueConstraint(
                fields=("student", "as_of_date"), name="balancesnapshot_student_as_of_unique"
            ),
            models.CheckConstraint(
                condition=Q(total_debits__gte=0), name="balancesnapshot_debits_gte_0"
            ),
            models.CheckConstraint(
                condition=Q(total_credits__lte=0), name="balancesnapshot_credits_lte_0"
            ),
        ]

    def __str__(self):
        return self.snapshot_id

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise AppendOnlyError(f"{self.snapshot_id}: snapshots are never edited")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise AppendOnlyError(f"{self.snapshot_id}: snapshots are never deleted")


class ReconciliationRun(models.Model):
    MUTABLE_FIELDS = ("status", "finished_at", "summary")

    run_id = models.CharField(
        max_length=ID_MAX_LENGTHS["reconciliation_run"],
        primary_key=True,
        validators=[id_validator("reconciliation_run")],
    )
    scope = models.CharField(max_length=8, choices=_choices(policy.SCOPES))
    student = models.ForeignKey(
        "students.Student",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reconciliation_runs",
    )
    as_of_date = models.DateField()
    status = models.CharField(max_length=10, choices=_choices(policy.RUN_STATUSES))
    started_at = models.DateTimeField()  # execution metadata; not exported
    finished_at = models.DateTimeField(null=True, blank=True)  # execution metadata
    triggered_by = models.CharField(max_length=10, choices=_choices(policy.TRIGGERS))
    summary = models.JSONField(default=dict)
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("run_id",)
        constraints = [
            models.CheckConstraint(
                condition=Q(scope__in=policy.SCOPES), name="reconciliationrun_scope_enum"
            ),
            models.CheckConstraint(
                condition=Q(status__in=policy.RUN_STATUSES), name="reconciliationrun_status_enum"
            ),
            models.CheckConstraint(
                condition=Q(triggered_by__in=policy.TRIGGERS),
                name="reconciliationrun_triggered_by_enum",
            ),
            models.CheckConstraint(
                condition=(Q(scope=policy.STUDENT) & Q(student__isnull=False))
                | (~Q(scope=policy.STUDENT) & Q(student__isnull=True)),
                name="reconciliationrun_student_iff_student_scope",
            ),
            models.CheckConstraint(
                condition=(Q(status=policy.RUNNING) & Q(finished_at__isnull=True))
                | (~Q(status=policy.RUNNING) & Q(finished_at__isnull=False)),
                name="reconciliationrun_finished_iff_not_running",
            ),
        ]

    def __str__(self):
        return self.run_id

    def save(self, *args, **kwargs):
        if not self._state.adding:
            update_fields = kwargs.get("update_fields")
            if update_fields is None or set(update_fields) - set(self.MUTABLE_FIELDS):
                raise AppendOnlyError(
                    f"{self.run_id}: only {', '.join(self.MUTABLE_FIELDS)} may change, "
                    "named in update_fields"
                )
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise AppendOnlyError(f"{self.run_id}: runs are never deleted")


class AnomalyFinding(models.Model):
    finding_id = models.CharField(
        max_length=ID_MAX_LENGTHS["anomaly_finding"],
        primary_key=True,
        validators=[id_validator("anomaly_finding")],
    )
    run = models.ForeignKey(ReconciliationRun, on_delete=models.PROTECT, related_name="findings")
    issue_type = models.CharField(max_length=36, choices=_choices(policy.ISSUE_TYPES))
    rule_id = models.CharField(max_length=12)
    severity = models.CharField(max_length=8, choices=_choices(policy.SEVERITIES))
    entity_type = models.CharField(max_length=24)
    record_id = models.CharField(max_length=64)
    student = models.ForeignKey(
        "students.Student",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="anomaly_findings",
    )
    expected_value = models.DecimalField(**MONEY, null=True, blank=True)
    actual_value = models.DecimalField(**MONEY, null=True, blank=True)
    details = models.JSONField(default=dict)
    recommended_action = models.CharField(
        max_length=24, choices=_choices(policy.RECOMMENDED_ACTIONS)
    )
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("finding_id",)
        indexes = [
            models.Index(fields=("run", "issue_type"), name="finding_run_issue_idx"),
            models.Index(fields=("student",), name="finding_student_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(issue_type__in=policy.ISSUE_TYPES), name="finding_issue_type_enum"
            ),
            models.CheckConstraint(
                condition=Q(severity__in=policy.SEVERITIES), name="finding_severity_enum"
            ),
            models.CheckConstraint(
                condition=Q(recommended_action__in=policy.RECOMMENDED_ACTIONS),
                name="finding_recommended_action_enum",
            ),
        ]

    def __str__(self):
        return self.finding_id

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise AppendOnlyError(f"{self.finding_id}: findings are never edited")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise AppendOnlyError(f"{self.finding_id}: findings are never deleted")
