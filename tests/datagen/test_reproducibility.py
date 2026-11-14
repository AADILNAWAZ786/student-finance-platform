"""verify_reproducibility (Phase 8, P8-D4): two sequential subprocess runs in isolated temporary
databases produce byte-identical exports; a difference is reported with its file, primary key,
column and values; and a run whose databases would fall outside its own directory is refused
before anything is migrated. The configured (owner's) databases are never opened."""

import csv
import json
import shutil
from pathlib import Path

import pytest
from django.core.management import CommandError, call_command

from datagen import export as ex
from datagen import reproducibility as rp

# the test databases must exist for the module fixture; the subprocess runs never touch them
pytestmark = pytest.mark.django_db(databases=["default", "ground_truth"])

GOLDEN_COMBINED = "229335b44f7f8d96056e2e5ec60af4e2e9fb64191c4ce732af32e02cee578db1"


@pytest.fixture(scope="module")
def export_dir(generated, tmp_path_factory, django_db_blocker):
    out = tmp_path_factory.mktemp("repro") / "a"
    with django_db_blocker.unblock():
        ex.export(out)
    return out


def _set_csv(path: Path, row: int, column: str, value: str) -> str:
    with open(path, encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))
    i = rows[0].index(column)
    rows[row][i] = value
    with open(path, "w", encoding="utf-8", newline="") as fh:
        csv.writer(fh, lineterminator="\n").writerows(rows)
    return rows[row][0]


# --- comparison ---------------------------------------------------------------------------------


def test_identical_exports_compare_equal_and_run_log_is_excluded(export_dir, tmp_path):
    b = tmp_path / "b"
    shutil.copytree(export_dir, b)
    (b / "run_log.json").write_text(json.dumps({"started_at": "another time", "host": "x"}))
    diffs, compared = rp.compare(export_dir, b)
    assert diffs == []
    assert compared == 53  # 52 checksummed files + dataset_manifest.json; run_log.json excluded


def test_a_table_difference_names_the_primary_key_column_and_values(export_dir, tmp_path):
    b = tmp_path / "b"
    shutil.copytree(export_dir, b)
    pk = _set_csv(b / "dataset/csv/payments_payment.csv", 3, "amount", "1.23")
    diffs, _ = rp.compare(export_dir, b)
    assert len(diffs) == 1
    assert diffs[0].startswith("dataset/csv/payments_payment.csv differs (table payments_payment)")
    assert f"primary key {pk}, column amount:" in diffs[0] and "'1.23' in run_b" in diffs[0]


def test_other_differences_name_the_first_line_and_missing_files(export_dir, tmp_path):
    b = tmp_path / "b"
    shutil.copytree(export_dir, b)
    m = b / "dataset_manifest.json"
    m.write_text(m.read_text().replace('"seed": 20261022', '"seed": 1'))
    (b / "reports/findings.csv").unlink()
    diffs, _ = rp.compare(export_dir, b)
    assert "only in run_a: reports/findings.csv" in diffs
    assert any(
        d.startswith("dataset_manifest.json differs: line ") and '"seed": 1' in d for d in diffs
    )


# --- full runs in subprocesses ------------------------------------------------------------------


def test_starter_is_reproducible_across_processes(tmp_path):
    work = tmp_path / "work"
    outcome = rp.reproduce(seed=20261022, scale="starter", workdir=work)
    assert outcome.identical and outcome.differences == []
    assert [r.combined_sha256 for r in outcome.runs] == [GOLDEN_COMBINED] * 2
    assert [r.rows for r in outcome.runs] == [13095, 13095]
    report = outcome.report_path.read_text()
    assert "Result: **REPRODUCIBLE**" in report and "Excluded: `run_log.json`" in report
    assert "| run_a | 1 |" in report and "| run_b | 2 |" in report  # different hash seeds
    assert sorted(p.name for p in work.iterdir()) == ["reproducibility_report.md"]  # cleaned


def test_a_forced_mismatch_fails_with_an_actionable_report(tmp_path, monkeypatch, capsys):
    real = rp._one_run

    def tampered(run, args, config):
        real(run, args, config)
        if run.name == "run_b":
            _set_csv(
                run.root / "export/dataset/csv/billing_invoice.csv", 1, "lifecycle_status", "VOID"
            )

    monkeypatch.setattr(rp, "_one_run", tampered)
    work = tmp_path / "work"
    with pytest.raises(CommandError, match=r"NOT REPRODUCIBLE: 1 difference\(s\) in 53 files"):
        call_command("verify_reproducibility", "--workdir", str(work))
    out = capsys.readouterr().out
    assert "billing_invoice.csv differs (table billing_invoice): primary key INV-" in out
    assert "column lifecycle_status:" in out and "'VOID' in run_b" in out
    report = (work / "reproducibility_report.md").read_text()
    assert "Result: **NOT REPRODUCIBLE**" in report and "kept for inspection" in report
    for run in ("run_a", "run_b"):  # kept: databases, export and the step logs
        assert (work / run / "db/default.sqlite3").is_file()
        assert (work / run / "logs/generate_data.log").is_file()


def test_databases_outside_the_run_directory_are_refused_before_migrating(tmp_path, monkeypatch):
    def unsafe(run):  # a settings module that keeps the configured database paths
        (run.root / "settings").mkdir(parents=True)
        (run.root / "settings/sf_unsafe.py").write_text("from config.settings import *  # noqa\n")
        return "sf_unsafe"

    monkeypatch.setattr(rp, "_settings_module", unsafe)
    work = tmp_path / "work"
    with pytest.raises(rp.ReproducibilityError, match="outside .*run_a; refusing"):
        rp.reproduce(seed=20261022, workdir=work)
    assert sorted(p.name for p in (work / "run_a/logs").iterdir()) == ["settings_probe.log"]


def test_refuses_a_non_empty_workdir_and_a_missing_config(tmp_path):
    (tmp_path / "x").write_text("mine")
    with pytest.raises(rp.ReproducibilityError, match="is not empty"):
        rp.reproduce(seed=1, workdir=tmp_path)
    with pytest.raises(rp.ReproducibilityError, match="does not exist"):
        rp.reproduce(seed=1, workdir=tmp_path / "new", config=str(tmp_path / "none.yaml"))
