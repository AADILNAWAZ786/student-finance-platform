"""export_data (Phase 8, P8-D2/D3; ARCHITECTURE §11.4, R-29, R-40): layout, byte-level formats,
manifest hashes, determinism, label separation, and every refusal. Starter dataset in the
test databases (never the owner's files)."""

import csv
import hashlib
import json
import re
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from django.core.management import CommandError, call_command

from datagen import export as ex
from datagen.config import load_raw
from datagen.models import GroundTruthLabel
from payments.models import Payment

pytestmark = pytest.mark.django_db(databases=["default", "ground_truth"])

TABLES = sorted(m._meta.db_table for m in ex.DATASET_MODELS)
# seed 20261022, starter: identical on Python 3.14 and 3.12 (with the Phase 7 goldens)
GOLDEN_COMBINED = "229335b44f7f8d96056e2e5ec60af4e2e9fb64191c4ce732af32e02cee578db1"
ISO_TIMESTAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d")


@pytest.fixture(scope="module")
def exported(generated, tmp_path_factory, django_db_blocker):
    out = tmp_path_factory.mktemp("export") / "starter"
    with django_db_blocker.unblock():
        result = ex.export(out)
    return result


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_layout_is_exactly_the_documented_one(exported):
    files = sorted(
        p.relative_to(exported.out).as_posix() for p in exported.out.rglob("*") if p.is_file()
    )
    expected = sorted(
        [f"dataset/csv/{t}.csv" for t in TABLES]
        + [f"dataset/json/{t}.json" for t in TABLES]
        + ["dataset/sql/schema.sql", "dataset/sql/seed.sql"]
        + ["ground_truth/ground_truth.csv", "ground_truth/ground_truth.json"]
        + ["reports/findings.csv", "reports/evaluation_report.json"]
        + ["reports/validation_summary.md", "source_manifest.json"]
        + ["dataset_manifest.json", "run_log.json"]
    )
    assert files == expected
    assert len(TABLES) == 22 and "core_idsequence" not in TABLES  # IdSequence is internal


def test_manifest_hashes_every_checksummed_file(exported):
    m = json.loads((exported.out / "dataset_manifest.json").read_text())
    assert set(m["files"]) == {
        p.relative_to(exported.out).as_posix()
        for p in exported.out.rglob("*")
        if p.is_file() and p.name not in ("dataset_manifest.json", "run_log.json")
    }
    for rel, info in m["files"].items():
        path = exported.out / rel
        assert info["sha256"] == _sha(path), rel
        assert info["bytes"] == path.stat().st_size, rel
    assert m["combined_sha256"] == ex.combined_digest(m["files"]) == GOLDEN_COMBINED
    assert m["seed"] == 20261022 and m["scale_name"] == "starter"
    assert m["row_counts"]["students_student"] == 100 and m["ground_truth_rows"] == 15


def test_deterministic_files_hold_no_execution_metadata(exported):
    m = json.loads((exported.out / "dataset_manifest.json").read_text())
    assert m["excluded_columns"] == ["finished_at", "recorded_at", "started_at"]
    for path in exported.out.rglob("*"):
        if path.is_file() and path.name not in ("run_log.json", "dataset_manifest.json"):
            text = path.read_text(encoding="utf-8")
            assert not ISO_TIMESTAMP.search(text), path.name
            assert not any(c in text for c in m["excluded_columns"]), path.name
    rest = json.dumps({k: v for k, v in m.items() if k != "excluded_columns"})
    assert not ISO_TIMESTAMP.search(rest) and "recorded_at" not in rest
    assert not {"host", "git_sha", "code_version", "output"} & set(m)
    log = json.loads((exported.out / "run_log.json").read_text())
    assert ISO_TIMESTAMP.search(log["started_at"]) and log["environment"]["python"]


def test_rows_match_the_database_and_formats_are_canonical(exported):
    m = json.loads((exported.out / "dataset_manifest.json").read_text())
    for model in ex.DATASET_MODELS:
        table = model._meta.db_table
        with open(exported.out / f"dataset/csv/{table}.csv", encoding="utf-8", newline="") as fh:
            reader = list(csv.reader(fh))
        assert reader[0] == [f.attname for f in ex.columns(model)]
        assert len(reader) - 1 == model.objects.count() == m["row_counts"][table]
        pks = [r[0] for r in reader[1:]]
        assert pks == sorted(pks)  # PK order
        text = (exported.out / f"dataset/json/{table}.json").read_text(encoding="utf-8")
        data = json.loads(text)
        assert text == json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        assert [str(r[model._meta.pk.attname]) for r in data] == pks
    payments = json.loads((exported.out / "dataset/json/payments_payment.json").read_text())
    assert all(re.fullmatch(r"\d+\.\d\d", p["amount"]) for p in payments)  # money as "1234.50"
    for path in exported.out.rglob("*"):
        if path.is_file():
            assert b"\r\n" not in path.read_bytes(), path.name  # LF only


def test_labels_appear_only_under_ground_truth(exported):
    label_ids = set(
        GroundTruthLabel.objects.using("ground_truth").values_list("label_id", flat=True)
    )
    for path in (exported.out / "dataset").rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            assert not any(x in text for x in ("GT-2026-", "groundtruth", "injection_method")), path
    gt = json.loads((exported.out / "ground_truth/ground_truth.json").read_text())
    assert {x["label_id"] for x in gt} == label_ids and len(gt) == 15


def test_schema_and_seed_load_into_an_empty_database(exported):
    import sqlite3

    db = sqlite3.connect(":memory:")
    db.executescript((exported.out / "dataset/sql/schema.sql").read_text(encoding="utf-8"))
    db.executescript((exported.out / "dataset/sql/seed.sql").read_text(encoding="utf-8"))
    m = json.loads((exported.out / "dataset_manifest.json").read_text())
    for table, n in m["row_counts"].items():
        assert db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] == n
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_a_second_export_is_byte_identical(exported, tmp_path):
    again = ex.export(tmp_path / "again")
    for rel, info in exported.manifest["files"].items():
        assert _sha(again.out / rel) == info["sha256"], rel
    assert _sha(again.out / "dataset_manifest.json") == _sha(exported.out / "dataset_manifest.json")


def test_reports_are_the_evaluation_of_the_datagen_run(exported):
    ev = json.loads((exported.out / "reports/evaluation_report.json").read_text())
    assert ev["meets_targets"] is True and ev["reconciliation_run"] == "RECON-2026-000001"
    with open(exported.out / "reports/findings.csv", encoding="utf-8") as fh:
        assert sum(1 for _ in fh) - 1 == 18
    summary = (exported.out / "reports/validation_summary.md").read_text()
    assert "Result: **PASSED**" in summary and "| students | 100 | 100 |" in summary
    source = json.loads((exported.out / "source_manifest.json").read_text())
    assert source["external_sources"] == [] and "example.edu" in source["personal_data"]


# --- refusals ------------------------------------------------------------------------------------


def test_refuses_a_non_empty_output_directory(generated, tmp_path):
    (tmp_path / "keep.txt").write_text("mine")
    with pytest.raises(ex.ExportError, match="already exists and is not an empty directory"):
        ex.export(tmp_path)
    assert [p.name for p in tmp_path.iterdir()] == ["keep.txt"]


def test_refuses_data_modified_after_generation(generated, tmp_path):
    p = Payment.objects.order_by("pk").first()
    Payment.objects.filter(pk=p.pk).update(amount=p.amount + Decimal("1.00"))
    with pytest.raises(ex.ExportError, match="changed tables: payments_payment"):
        ex.export(tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_refuses_a_different_config(generated, tmp_path):
    raw = load_raw()
    raw["fees"]["LIBRARY"] = 2500
    alt = tmp_path / "alt.yaml"
    alt.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ex.ExportError, match="pass the original file with --config"):
        ex.export(tmp_path / "out", config_path=alt)


def test_a_failure_part_way_leaves_nothing(generated, tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(ex, "_write_seed", boom)
    with pytest.raises(OSError, match="disk full"):
        ex.export(tmp_path / "out")
    assert list(tmp_path.iterdir()) == []  # no output, no partial directory


def test_the_command_reports_and_refuses(generated, tmp_path, capsys):
    call_command("export_data", "--out", str(tmp_path / "cmd"))
    out = capsys.readouterr().out
    assert f"combined_sha256={GOLDEN_COMBINED}" in out
    with pytest.raises(CommandError, match="already exists"):
        call_command("export_data", "--out", str(tmp_path / "cmd"))
