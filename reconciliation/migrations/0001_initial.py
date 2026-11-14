# Phase 6 (DATABASE_DESIGN §3 reconciliation): three new tables, constraints folded in.

import django.core.validators
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("students", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="ReconciliationRun",
            fields=[
                (
                    "run_id",
                    models.CharField(
                        max_length=17,
                        primary_key=True,
                        serialize=False,
                        validators=[
                            django.core.validators.RegexValidator(
                                code="invalid_reconciliation_run_id",
                                message="Enter a valid reconciliation run ID.",
                                regex="\\ARECON-\\d{4}-\\d{6}\\Z",
                            )
                        ],
                    ),
                ),
                (
                    "scope",
                    models.CharField(
                        choices=[("ALL", "ALL"), ("STUDENT", "STUDENT")], max_length=8
                    ),
                ),
                ("as_of_date", models.DateField()),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("RUNNING", "RUNNING"),
                            ("COMPLETED", "COMPLETED"),
                            ("FAILED", "FAILED"),
                        ],
                        max_length=10,
                    ),
                ),
                ("started_at", models.DateTimeField()),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                (
                    "triggered_by",
                    models.CharField(
                        choices=[("API", "API"), ("COMMAND", "COMMAND"), ("DATAGEN", "DATAGEN")],
                        max_length=10,
                    ),
                ),
                ("summary", models.JSONField(default=dict)),
                ("recorded_at", models.DateTimeField(auto_now_add=True)),
                (
                    "student",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="reconciliation_runs",
                        to="students.student",
                    ),
                ),
            ],
            options={
                "ordering": ("run_id",),
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(("scope__in", ("ALL", "STUDENT"))),
                        name="reconciliationrun_scope_enum",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("status__in", ("RUNNING", "COMPLETED", "FAILED"))),
                        name="reconciliationrun_status_enum",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("triggered_by__in", ("API", "COMMAND", "DATAGEN"))),
                        name="reconciliationrun_triggered_by_enum",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            models.Q(("scope", "STUDENT"), ("student__isnull", False)),
                            models.Q(
                                models.Q(("scope", "STUDENT"), _negated=True),
                                ("student__isnull", True),
                            ),
                            _connector="OR",
                        ),
                        name="reconciliationrun_student_iff_student_scope",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            models.Q(("status", "RUNNING"), ("finished_at__isnull", True)),
                            models.Q(
                                models.Q(("status", "RUNNING"), _negated=True),
                                ("finished_at__isnull", False),
                            ),
                            _connector="OR",
                        ),
                        name="reconciliationrun_finished_iff_not_running",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="AnomalyFinding",
            fields=[
                (
                    "finding_id",
                    models.CharField(
                        max_length=15,
                        primary_key=True,
                        serialize=False,
                        validators=[
                            django.core.validators.RegexValidator(
                                code="invalid_anomaly_finding_id",
                                message="Enter a valid anomaly finding ID.",
                                regex="\\AFND-\\d{4}-\\d{6}\\Z",
                            )
                        ],
                    ),
                ),
                (
                    "issue_type",
                    models.CharField(
                        choices=[
                            ("PAYMENT_OVER_ALLOCATED", "PAYMENT_OVER_ALLOCATED"),
                            (
                                "SCHOLARSHIP_EXCEEDS_ELIGIBLE_FEES",
                                "SCHOLARSHIP_EXCEEDS_ELIGIBLE_FEES",
                            ),
                            ("DUPLICATE_INVOICE", "DUPLICATE_INVOICE"),
                            ("REFUND_EXCEEDS_NET_PAID", "REFUND_EXCEEDS_NET_PAID"),
                            ("BALANCE_LEDGER_MISMATCH", "BALANCE_LEDGER_MISMATCH"),
                            ("ORPHAN_FK", "ORPHAN_FK"),
                            ("RELATIONSHIP_MISMATCH", "RELATIONSHIP_MISMATCH"),
                            ("INVALID_ENUM", "INVALID_ENUM"),
                            ("INVALID_ID_FORMAT", "INVALID_ID_FORMAT"),
                            ("DATE_ORDER", "DATE_ORDER"),
                            ("ARITHMETIC_MISMATCH", "ARITHMETIC_MISMATCH"),
                            ("ILLEGAL_TRANSITION", "ILLEGAL_TRANSITION"),
                            ("DUPLICATE_RECORD", "DUPLICATE_RECORD"),
                            ("INVOICE_STATUS_MISMATCH", "INVOICE_STATUS_MISMATCH"),
                        ],
                        max_length=36,
                    ),
                ),
                ("rule_id", models.CharField(max_length=12)),
                (
                    "severity",
                    models.CharField(
                        choices=[
                            ("CRITICAL", "CRITICAL"),
                            ("HIGH", "HIGH"),
                            ("MEDIUM", "MEDIUM"),
                            ("LOW", "LOW"),
                        ],
                        max_length=8,
                    ),
                ),
                ("entity_type", models.CharField(max_length=24)),
                ("record_id", models.CharField(max_length=64)),
                (
                    "expected_value",
                    models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True),
                ),
                (
                    "actual_value",
                    models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True),
                ),
                ("details", models.JSONField(default=dict)),
                (
                    "recommended_action",
                    models.CharField(
                        choices=[
                            ("REJECT_ALLOCATION", "REJECT_ALLOCATION"),
                            ("CAP_SCHOLARSHIP", "CAP_SCHOLARSHIP"),
                            ("VOID_DUPLICATE", "VOID_DUPLICATE"),
                            ("REVERSE_REFUND", "REVERSE_REFUND"),
                            ("RESTATE_BALANCE", "RESTATE_BALANCE"),
                            ("INVESTIGATE", "INVESTIGATE"),
                        ],
                        max_length=24,
                    ),
                ),
                ("recorded_at", models.DateTimeField(auto_now_add=True)),
                (
                    "student",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="anomaly_findings",
                        to="students.student",
                    ),
                ),
                (
                    "run",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="findings",
                        to="reconciliation.reconciliationrun",
                    ),
                ),
            ],
            options={
                "ordering": ("finding_id",),
                "indexes": [
                    models.Index(fields=["run", "issue_type"], name="finding_run_issue_idx"),
                    models.Index(fields=["student"], name="finding_student_idx"),
                ],
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(
                            (
                                "issue_type__in",
                                (
                                    "PAYMENT_OVER_ALLOCATED",
                                    "SCHOLARSHIP_EXCEEDS_ELIGIBLE_FEES",
                                    "DUPLICATE_INVOICE",
                                    "REFUND_EXCEEDS_NET_PAID",
                                    "BALANCE_LEDGER_MISMATCH",
                                    "ORPHAN_FK",
                                    "RELATIONSHIP_MISMATCH",
                                    "INVALID_ENUM",
                                    "INVALID_ID_FORMAT",
                                    "DATE_ORDER",
                                    "ARITHMETIC_MISMATCH",
                                    "ILLEGAL_TRANSITION",
                                    "DUPLICATE_RECORD",
                                    "INVOICE_STATUS_MISMATCH",
                                ),
                            )
                        ),
                        name="finding_issue_type_enum",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("severity__in", ("CRITICAL", "HIGH", "MEDIUM", "LOW"))),
                        name="finding_severity_enum",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            (
                                "recommended_action__in",
                                (
                                    "REJECT_ALLOCATION",
                                    "CAP_SCHOLARSHIP",
                                    "VOID_DUPLICATE",
                                    "REVERSE_REFUND",
                                    "RESTATE_BALANCE",
                                    "INVESTIGATE",
                                ),
                            )
                        ),
                        name="finding_recommended_action_enum",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="BalanceSnapshot",
            fields=[
                (
                    "snapshot_id",
                    models.CharField(
                        max_length=16,
                        primary_key=True,
                        serialize=False,
                        validators=[
                            django.core.validators.RegexValidator(
                                code="invalid_balance_snapshot_id",
                                message="Enter a valid balance snapshot ID.",
                                regex="\\ASNAP-\\d{4}-\\d{6}\\Z",
                            )
                        ],
                    ),
                ),
                ("as_of_date", models.DateField()),
                ("reported_balance", models.DecimalField(decimal_places=2, max_digits=12)),
                (
                    "total_debits",
                    models.DecimalField(
                        decimal_places=2, help_text="Σ positive entries ≤ as_of_date", max_digits=12
                    ),
                ),
                (
                    "total_credits",
                    models.DecimalField(
                        decimal_places=2, help_text="Σ negative entries ≤ as_of_date", max_digits=12
                    ),
                ),
                ("recorded_at", models.DateTimeField(auto_now_add=True)),
                (
                    "student",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="balance_snapshots",
                        to="students.student",
                    ),
                ),
            ],
            options={
                "ordering": ("snapshot_id",),
                "constraints": [
                    models.UniqueConstraint(
                        fields=("student", "as_of_date"),
                        name="balancesnapshot_student_as_of_unique",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("total_debits__gte", 0)),
                        name="balancesnapshot_debits_gte_0",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("total_credits__lte", 0)),
                        name="balancesnapshot_credits_lte_0",
                    ),
                ],
            },
        ),
    ]
