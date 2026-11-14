"""verify_export (Phase 8, P8-D5): a clean export passes every check; each kind of tampering is
caught by the check responsible for it. The verifier reads files only: no database is written."""

import csv
import hashlib
import json
import shutil
from pathlib import Path

import pytest
from django.core.management import CommandError, call_command

from datagen import export as ex
from datagen import verification as vf

pytestmark = pytest.mark.django_db(databases=["default", "ground_truth"])

PAYMENTS = "dataset/csv/payments_payment.csv"


@pytest.fixture(scope="module")
def clean(generated, tmp_path_factory, django_db_blocker):
    out = tmp_path_factory.mktemp("verify") / "starter"
    with django_db_blocker.unblock():
        ex.export(out)
    return out


@pytest.fixture
def copy(clean, tmp_path):
    target = tmp_path / "copy"
    shutil.copytree(clean, target)
    return target


def _failed(report) -> set[str]:
    return {name for name, c in report.checks.items() if not c.ok}


def _rehash(out: Path, *rels: str) -> None:
    """Make the manifest agree with edited files, so only the deeper checks can catch the edit."""
    path = out / "dataset_manifest.json"
    m = json.loads(path.read_text())
    for rel in rels:
        data = (out / rel).read_bytes()
        m["files"][rel]["sha256"] = hashlib.sha256(data).hexdigest()
        m["files"][rel]["bytes"] = len(data)
    m["combined_sha256"] = ex.combined_digest(m["files"])
    path.write_text(json.dumps(m, indent=2, sort_keys=True) + "\n")


def _edit_csv(path: Path, edit) -> None:
    with open(path, encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))
    edit(rows)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        csv.writer(fh, lineterminator="\n").writerows(rows)


def test_a_clean_export_passes_every_check(clean):
    report = vf.verify(clean)
    assert report.ok, report.lines()
    assert list(report.checks) == [
        "manifest",
        "files",
        "row_counts",
        "csv_json",
        "csv_sql",
        "ids",
        "foreign_keys",
        "labels",
        "separation",
        "reports",
    ]
    assert report.tables == 22 and report.rows == 13095


def test_run_log_is_not_checked(copy):
    (copy / "run_log.json").write_text('{"anything": "goes"}\n')
    assert vf.verify(copy).ok


def test_a_changed_byte_is_a_hash_mismatch(copy):
    path = copy / PAYMENTS
    path.write_bytes(path.read_bytes().replace(b"PAY-2026-000001", b"PAY-2026-000X01", 1))
    report = vf.verify(copy)
    assert "files" in _failed(report)
    assert any("payments_payment.csv: sha256" in p for p in report.checks["files"].problems)


def test_missing_and_unlisted_files(copy):
    (copy / "dataset/json/academics_term.json").unlink()
    (copy / "notes.txt").write_text("extra")
    problems = vf.verify(copy).checks["files"].problems
    assert "missing file dataset/json/academics_term.json" in problems
    assert "unlisted file notes.txt" in problems


def test_csv_edited_with_a_consistent_manifest_is_caught_by_json_and_sql(copy):
    def edit(rows):
        i = rows[0].index("amount")
        rows[1][i] = "99999.99"

    _edit_csv(copy / PAYMENTS, edit)
    _rehash(copy, PAYMENTS)
    report = vf.verify(copy)
    assert _failed(report) == {"csv_json", "csv_sql"}
    assert "amount is" in report.checks["csv_json"].problems[0]


def test_a_dropped_row_is_a_count_mismatch(copy):
    _edit_csv(copy / PAYMENTS, lambda rows: rows.pop())
    _rehash(copy, PAYMENTS)
    report = vf.verify(copy)
    assert "row_counts" in _failed(report)
    assert any("payments_payment" in p for p in report.checks["row_counts"].problems)


def test_a_duplicate_id_and_a_dangling_foreign_key(copy):
    rel = "dataset/csv/payments_paymentallocation.csv"

    def edit(rows):
        i = rows[0].index("invoice_line_id")
        rows[2][0] = rows[1][0]  # duplicate primary key
        rows[3][i] = "INV-STU-999999-2026-000001-L01"  # no such invoice line

    _edit_csv(copy / rel, edit)
    _rehash(copy, rel)
    report = vf.verify(copy)
    assert {"ids", "foreign_keys"} <= _failed(report)
    assert "duplicate primary key" in report.checks["ids"].problems[0]
    assert any(
        "invoice_line_id=INV-STU-999999-2026-000001-L01 has no row in billing_invoiceline" in p
        for p in report.checks["foreign_keys"].problems
    )


def test_sql_that_does_not_load(copy):
    rel = "dataset/sql/schema.sql"
    path = copy / rel
    path.write_text(path.read_text().replace("CREATE TABLE", "CREATE TABEL", 1))
    _rehash(copy, rel)
    report = vf.verify(copy)
    assert _failed(report) == {"csv_sql"}
    assert "do not load" in report.checks["csv_sql"].problems[0]


def test_a_label_leaked_into_the_dataset(copy):
    rel = "dataset/csv/students_student.csv"

    def edit(rows):
        rows[1][-1] = "GT-2026-000001"

    _edit_csv(copy / rel, edit)
    _rehash(copy, rel)
    report = vf.verify(copy)
    assert "separation" in _failed(report)
    assert "contains label id GT-2026-000001" in report.checks["separation"].problems[0]


def test_a_ground_truth_file_copied_into_dataset(copy):
    shutil.copy(copy / "ground_truth/ground_truth.csv", copy / "dataset/csv/ground_truth.csv")
    m = json.loads((copy / "dataset_manifest.json").read_text())
    m["files"]["dataset/csv/ground_truth.csv"] = {"bytes": 0, "rows": 0, "sha256": ""}
    (copy / "dataset_manifest.json").write_text(json.dumps(m))
    _rehash(copy, "dataset/csv/ground_truth.csv")
    report = vf.verify(copy)
    problems = report.checks["separation"].problems
    assert "dataset/csv/ground_truth.csv is not a dataset table file" in problems
    assert any("has label columns" in p for p in problems)


def test_a_label_pointing_at_a_missing_record(copy):
    rel = "ground_truth/ground_truth.json"
    data = json.loads((copy / rel).read_text())
    data[0]["record_id"] = "SNAP-2026-999999"
    (copy / rel).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    _rehash(copy, rel)
    problems = vf.verify(copy).checks["labels"].problems
    assert any(
        "record SNAP-2026-999999 is not in reconciliation_balancesnapshot" in p for p in problems
    )
    assert any("record_id is" in p for p in problems)  # JSON no longer equals the CSV


def test_findings_count_against_the_manifest(copy):
    rel = "reports/findings.csv"
    _edit_csv(copy / rel, lambda rows: rows.pop())
    _rehash(copy, rel)
    assert _failed(vf.verify(copy)) == {"reports"}


def test_an_unknown_manifest_version_stops_early(copy):
    path = copy / "dataset_manifest.json"
    m = json.loads(path.read_text())
    m["format_version"] = 2
    path.write_text(json.dumps(m))
    report = vf.verify(copy)
    assert _failed(report) == {"manifest"}


def test_not_an_export(tmp_path):
    with pytest.raises(vf.VerificationError, match="no readable dataset_manifest.json"):
        vf.verify(tmp_path)


def test_the_command_passes_and_fails(clean, copy, capsys):
    call_command("verify_export", str(clean))
    assert "export verification PASSED: 22 tables, 13095 dataset rows" in capsys.readouterr().out
    (copy / PAYMENTS).write_text("tampered\n")
    with pytest.raises(CommandError, match=r"FAILED \(files, csv_json"):
        call_command("verify_export", str(copy))
    assert "FAIL  files" in capsys.readouterr().out


def test_verification_never_queries_a_database(clean):
    from django.db import connections
    from django.test.utils import CaptureQueriesContext

    with (
        CaptureQueriesContext(connections["default"]) as default,
        CaptureQueriesContext(connections["ground_truth"]) as gt,
    ):
        assert vf.verify(clean).ok
    assert len(default) == 0 and len(gt) == 0  # files only: no import, no reads
