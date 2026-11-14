"""Full scale, end to end (Phase 8, P8-D7). Opt-in: SF_FULLSCALE=1 (about 12 minutes and up to
~4.4 GB of memory on a 2-core machine). Runs verify_reproducibility at full scale (two
sequential subprocess runs, each with fresh SQLite files in a temporary directory, never the
configured databases), then verify_export on the kept export of the first run."""

import json
import os
try:
    import resource
except ImportError:  # Windows does not provide the resource module.
    resource = None
import time

import pytest

from datagen import reproducibility as rp
from datagen import verification as vf

pytestmark = pytest.mark.skipif(
    os.environ.get("SF_FULLSCALE") != "1", reason="full scale is opt-in: SF_FULLSCALE=1"
)

# DATABASE_DESIGN §7 exact volumes at full scale (including the 150 injected rows per type)
EXACT = {
    "academics_program": 20,
    "academics_term": 12,
    "scholarships_scholarshiptype": 7,
    "scholarships_scholarshiptypeprogram": 8,
    "students_student": 10_000,
    "academics_enrollment": 30_000,
    "billing_invoice": 30_150,
    "scholarships_eligibility": 210_000,
    "scholarships_scholarship": 15_150,
    "payments_payment": 50_000,
    "reconciliation_balancesnapshot": 40_000,
}


def test_full_scale_is_reproducible_and_its_export_verifies(tmp_path):
    work = tmp_path / "full"
    outcome = rp.reproduce(seed=20261022, scale="full", workdir=work, keep=True)
    assert outcome.identical, outcome.differences
    a, b = outcome.runs
    assert a.combined_sha256 == b.combined_sha256 and a.rows == b.rows > 1_000_000

    t0 = time.monotonic()
    report = vf.verify(work / "run_a" / "export")
    verify_s = time.monotonic() - t0
    assert report.ok, report.lines()
    for run in outcome.runs:  # recorded in docs/phase8_verification_results.txt
        print(f"{run.name}: {run.combined_sha256} rows={run.rows} steps={run.steps}")
    print(f"verify_export: {verify_s:.1f}s, {report.tables} tables, {report.rows} rows")
    if resource is None:
        print(
            f"peak RSS: child {outcome.peak_rss_mb:.0f} MB; "
            "verifier process measurement unavailable on this platform"
        )
    else:
        self_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        print(
            f"peak RSS: child {outcome.peak_rss_mb:.0f} MB, "
            f"verifier process {self_mb:.0f} MB"
        )

    manifest = json.loads((work / "run_a/export/dataset_manifest.json").read_text())
    for table, n in EXACT.items():
        assert manifest["row_counts"][table] == n, table
    assert manifest["ground_truth_rows"] == 750  # 150 per anomaly type
