"""ARCHITECTURE §16 Phase 7 exit criteria at starter scale: exact baseline counts (DATABASE_DESIGN
§7), coverage bands and boundary cases, stored checksums equal to the planned ones, and the
evaluation targets (§12.3): precision = recall = 1 per type, 0 unexpected, 100 % of the
non-anomalous students reconciled."""

import re
from collections import Counter
from datetime import date
from decimal import Decimal

import pytest

from core.models import IdSequence
from datagen import services
from datagen.models import REQUIRED_ISSUE_TYPES, GenerationRun, GroundTruthLabel
from datagen.pipeline.materialize import MODELS
from reconciliation.checks import REGISTRY

pytestmark = pytest.mark.django_db(databases=["default", "ground_truth"])

STARTER_BASELINE = {  # DATABASE_DESIGN §7 "Baseline (starter)" exact values
    "academics_program": 20,
    "academics_term": 12,
    "scholarships_scholarshiptype": 7,
    "scholarships_scholarshiptypeprogram": 8,
    "students_student": 100,
    "academics_enrollment": 300,
    "scholarships_eligibility": 2100,
    "payments_payment": 500,
    "reconciliation_balancesnapshot": 400,
}


def test_exact_starter_counts_including_three_injections_per_type(generated):
    counts = generated.run.row_counts
    for table, n in STARTER_BASELINE.items():
        assert counts[table] == n, table
    # baseline 300 invoices / 150 awards, plus 3 injected of each (DATABASE_DESIGN §7)
    assert counts["billing_invoice"] == 303
    assert counts["scholarships_scholarship"] == 153
    assert GroundTruthLabel.objects.using("ground_truth").count() == 15
    by_type = Counter(
        GroundTruthLabel.objects.using("ground_truth").values_list("issue_type", flat=True)
    )
    assert by_type == {t: 3 for t in REQUIRED_ISSUE_TYPES}


def test_row_counts_are_the_stored_rows(generated):
    for model in MODELS:
        if model is IdSequence:
            continue
        assert generated.run.row_counts[model._meta.db_table] == model.objects.count()


def test_validation_passed_every_band_and_boundary_case(generated):
    v = generated.plan.validation
    assert v["passed"] and v["problems"] == []
    assert {k: x["actual"] for k, x in v["exact_counts"].items()} == {
        k: x["expected"] for k, x in v["exact_counts"].items()
    }
    for name, band in v["coverage"].items():
        assert Decimal(band["actual_pct"]) >= Decimal(band["low_pct"]), name
        if band["high_pct"] is not None:
            assert Decimal(band["actual_pct"]) <= Decimal(band["high_pct"]), name
    assert v["count_targets"]["full_ride_awards"]["actual"] in range(3, 7)
    assert all(n >= 1 for n in v["boundary_cases"]["counts"].values())
    assert len(v["boundary_cases"]["counts"]) == 15


def test_stored_checksums_equal_the_planned_ones(generated):
    run = GenerationRun.objects.get()
    stored = services.database_checksums()
    assert stored == run.checksums == generated.plan.checksums
    assert services.stored_label_checksum(run.pk) == generated.plan.labels_checksum


def test_generation_run_metadata(generated):
    run = GenerationRun.objects.get()
    assert (run.run_id, run.seed, run.scale_name, run.scale_factor) == (
        "GEN-2026-0001",
        20261022,
        "starter",
        Decimal("0.0100"),
    )
    assert run.config_hash == generated.plan.settings.hash and len(run.config_hash) == 64
    assert run.started_at <= run.finished_at


def test_id_sequences_end_at_the_highest_stored_counter(generated):
    """Every committed counter has its row (no gaps, no unused tail; S-ID-GAPS stays clean)."""
    seqs = dict(IdSequence.objects.values_list("key", "last_value"))
    assert not any(k.startswith("GT") for k in seqs)  # D-G3: label IDs never come from here
    assert seqs["STU"] == 100 and seqs["PRG"] == 20 and seqs["SCT"] == 7
    assert seqs["PAY:2026"] == 500 and seqs["GEN:2026"] == 1
    from billing.models import Invoice, LedgerEntry

    led = max(int(x[-7:]) for x in LedgerEntry.objects.values_list("entry_id", flat=True))
    assert seqs["LED:2026"] == led == LedgerEntry.objects.count()
    for sid, n in Counter(Invoice.objects.values_list("student_id", flat=True)).items():
        assert seqs[f"INV:{sid}:2026"] == n


def test_every_business_date_and_id_is_in_2026(generated):
    from billing.models import InvoiceLine, LedgerEntry
    from payments.models import Payment

    for model, field in (
        (LedgerEntry, "entry_date"),
        (InvoiceLine, "posted_date"),
        (Payment, "received_date"),
    ):
        years = {d.year for d in model.objects.values_list(field, flat=True)}
        assert years == {2026}
    year_scoped = re.compile(r"-(\d{4})-")
    for model in MODELS:
        if model is IdSequence:
            continue
        for pk in model.objects.values_list("pk", flat=True):
            for y in year_scoped.findall(str(pk)):
                assert y in ("2026", "2023", "2024", "2025") and (
                    y == "2026" or model._meta.db_table == "academics_term"
                ), pk


def test_the_blind_run_and_the_evaluation_meet_every_target(generated):
    recon = generated.reconciliation_run
    assert (recon.status, recon.scope, recon.triggered_by) == ("COMPLETED", "ALL", "DATAGEN")
    assert recon.as_of_date == date(2026, 12, 31)
    assert len(recon.summary["checks"]) == len(REGISTRY) == 63
    ev = generated.evaluation
    for issue_type in REQUIRED_ISSUE_TYPES:
        m = ev["by_type"][issue_type]
        assert (m["labels"], m["tp"], m["fp"], m["fn"]) == (3, 3, 0, 0), issue_type
        assert (m["precision"], m["recall"], m["f1"]) == ("1.0000", "1.0000", "1.0000")
    assert ev["overall"]["tp"] == 15 and ev["overall"]["findings"] == 18
    assert ev["unexpected"] == 0 and ev["unexpected_findings"] == []
    assert ev["students"]["pct_non_anomalous_reconciled"] == "100.00"
    assert ev["students"]["non_anomalous"] == 85
    assert ev["meets_targets"] is True


def test_the_three_consequential_findings_are_the_refund_anomalies_payments(generated):
    """D-G4 / P6-O3: each injected refund over-commits its source payment (BR-ALC-01)."""
    ev = generated.evaluation
    labels = {
        x.label_id: x
        for x in GroundTruthLabel.objects.using("ground_truth").filter(
            issue_type="REFUND_EXCEEDS_NET_PAID"
        )
    }
    assert ev["consequential"] == 3
    for c in ev["consequential_findings"]:
        assert c["issue_type"] == "PAYMENT_OVER_ALLOCATED"
        (label_id,) = c["caused_by_labels"]
        assert c["record_id"] == labels[label_id].related_ids[0]  # the source payment
    assert ev["by_type"]["PAYMENT_OVER_ALLOCATED"]["consequential"] == 3


def test_one_recorded_at_for_every_generated_row(generated):
    """Insertion timing cannot reveal injected rows (ARCHITECTURE §12.2.3)."""
    stamps = set()
    for model in MODELS:
        if model is IdSequence:
            continue
        stamps |= set(model.objects.values_list("recorded_at", flat=True).distinct())
    stamps |= set(
        GroundTruthLabel.objects.using("ground_truth").values_list("recorded_at", flat=True)
    )
    assert len(stamps) == 1
