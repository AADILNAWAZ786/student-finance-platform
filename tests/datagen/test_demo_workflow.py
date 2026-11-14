"""demo_workflow (Phase 9, P9-D1…D4): the complete workflow runs end to end in isolated
temporary databases, deterministically; it refuses anything but its own new databases, never
opens the configured ones, refuses a non-empty output directory, and exits non-zero when a stage
fails. Every run here is a real subprocess on new SQLite files under pytest's tmp_path."""

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
import yaml
from django.core.management import CommandError, call_command
from django.test import override_settings

from datagen import demo
from datagen.config import load_raw

STARTER_EXPORT = "229335b44f7f8d96056e2e5ec60af4e2e9fb64191c4ce732af32e02cee578db1"
SECRETS = ("token", "authorization", "cookie", "password", "csrf", "sessionid", "secret")


@pytest.fixture(scope="module")
def first(tmp_path_factory):
    out = tmp_path_factory.mktemp("demo") / "a"
    return out, demo.run_demo(out=out, seed=20261022, scale="starter")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _deterministic_report(path: Path) -> str:
    return path.read_text().split("\n## Execution (not deterministic)")[0]


# --- the workflow --------------------------------------------------------------------------------


def test_every_stage_passes_and_the_figures_are_right(first):
    out, res = first
    assert res["stages"] == {s: "PASS" for s in demo.STAGES} and res["exit_code"] == 0
    a, b, c = res["A_dataset"], res["B_export"], res["E_api"]
    assert (a["generation_run"], a["reconciliation_run"]) == ("GEN-2026-0001", "RECON-2026-000001")
    assert a["row_counts"]["students_student"] == 100 and a["seed"] == 20261022
    assert b["combined_sha256"] == STARTER_EXPORT  # the Phase 8 golden, reproduced
    assert all(b["verify_export"].values()) and len(b["verify_export"]) == 10
    # the §9.3 IDs on the starter dataset (100 students, 300 enrollments, 500 payments)
    assert (c["student"], c["enrollment"], c["invoice"]) == (
        "STU-000101",
        "ENR-2026-000301",
        "INV-STU-000101-2026-000001",
    )
    assert c["payment"] == "PAY-2026-000501" and c["allocations"][0] == "ALLOC-PAY-2026-000501-01"
    assert c["final_balance"] == "0.00" and c["student_findings"] == 0
    assert c["cancellation_invoice_status"] == "CANCELLED"
    assert c["withdrawal_refund_pct"] == "0.60"  # day 13 after the 2026-09-07 start
    assert c["late_fee_invoice"] in c["late_fee_dry_run_assessed"]
    d, e = res["C_anomalies"], res["D_evaluation"]
    assert set(d["by_type"]) == set(e["by_type"]) and len(d["by_type"]) == 5
    for issue, v in d["by_type"].items():
        assert v["labels"] == 3 and v["example_label"].startswith("GT-2026-"), issue
        assert v["example_finding"].startswith("FND-"), issue  # the labelled record was detected
    assert d["by_type"]["PAYMENT_OVER_ALLOCATED"]["findings"] == 6  # 3 labelled + 3 consequential
    for v in e["by_type"].values():
        assert (v["tp"], v["fp"], v["fn"]) == (3, 0, 0)
        assert v["precision"] == v["recall"] == v["f1"] == "1.0000"
    assert e["meets_targets"] is True
    # evaluated before the API stage adds students: exactly the generated dataset (P9-O3)
    assert e["students"]["total"] == 100 and e["students"]["non_anomalous"] == 85
    assert e["students"]["pct_non_anomalous_reconciled"] == "100.00"
    assert res["api_calls"] == 63 and res["api_refusals"] == 7


def test_every_expected_refusal_is_captured_with_its_code(first):
    out, _ = first
    calls = json.loads((out / "api_transcript.json").read_text())["calls"]
    refusals = {
        (c["response"]["status"], c["response"]["body"]["error"]["code"])
        for c in calls
        if c["expected_status"] >= 400
    }
    assert refusals == {
        (409, "DUPLICATE_INVOICE"),
        (422, "SCHOLARSHIP_EXCEEDS_ELIGIBLE_FEES"),
        (422, "ALLOCATION_EXCEEDS_PAYMENT"),
        (422, "REFUND_EXCEEDS_CREDIT_BALANCE"),
        (409, "ILLEGAL_TRANSITION"),
    }
    assert all(c["response"]["status"] == c["expected_status"] for c in calls)


def test_the_transcript_holds_no_credentials_and_masks_only_execution_times(first):
    out, _ = first
    text = (out / "api_transcript.json").read_text()
    data = json.loads(text)
    assert not any(word in text.lower() for word in SECRETS)
    assert all(
        set(c) == {"n", "stage", "title", "request", "response", "expected_status"}
        for c in data["calls"]
    )
    assert all(set(c["request"]) == {"method", "path", "query", "body"} for c in data["calls"])
    assert data["masked_keys"] == ["finished_at", "recorded_at", "started_at"]

    def masked_keys(value, found):
        if isinstance(value, dict):
            for k, v in value.items():
                if v == demo.VOLATILE:
                    found.add(k)
                masked_keys(v, found)
        elif isinstance(value, list):
            for v in value:
                masked_keys(v, found)
        return found

    assert masked_keys(data["calls"], set()) <= demo.VOLATILE_KEYS


def test_outputs_and_cleanup(first):
    out, res = first
    for name in ("demo_report.md", "api_transcript.json", "demo_results.json", "logs/timings.json"):
        assert (out / name).is_file(), name
    assert (out / "export/ground_truth/ground_truth.json").is_file()
    assert not (out / "run/db").exists() and res["kept_databases"] is False  # removed unless --keep
    report = (out / "demo_report.md").read_text()
    assert "| E_api | PASS |" in report and "## Execution (not deterministic)" in report


def test_the_same_seed_gives_the_same_demo(first, tmp_path):
    out_a, _ = first
    out_b = tmp_path / "b"
    demo.run_demo(out=out_b, seed=20261022, scale="starter")
    for name in ("api_transcript.json", "demo_results.json"):
        assert _sha(out_a / name) == _sha(out_b / name), name
    assert _deterministic_report(out_a / "demo_report.md") == _deterministic_report(
        out_b / "demo_report.md"
    )
    manifest = json.loads((out_a / "export/dataset_manifest.json").read_text())
    for rel, info in manifest["files"].items():
        assert _sha(out_b / "export" / rel) == info["sha256"], rel


# --- safety --------------------------------------------------------------------------------------


def test_the_configured_databases_are_never_opened(tmp_path):
    """Base settings whose configured databases are sentinel files: the demo runs on top of them
    and leaves them byte for byte as they were, with no -wal / -shm beside them."""
    owner = tmp_path / "owner"
    owner.mkdir()
    for name in ("db.sqlite3", "ground_truth.sqlite3"):
        con = sqlite3.connect(owner / name)
        con.execute("CREATE TABLE sentinel (x TEXT)")
        con.execute("INSERT INTO sentinel VALUES ('owner data')")
        con.commit()
        con.close()
    before = {p.name: _sha(p) for p in owner.iterdir()}
    mod = tmp_path / "mods"
    mod.mkdir()
    (mod / "sf_owner_settings.py").write_text(
        "from config.settings import *  # noqa\n"
        "from config.settings import DATABASES\n"
        f"DATABASES['default']['NAME'] = {str(owner / 'db.sqlite3')!r}\n"
        f"DATABASES['ground_truth']['NAME'] = {str(owner / 'ground_truth.sqlite3')!r}\n"
    )
    res = demo.run_demo(
        out=tmp_path / "out",
        seed=20261022,
        scale="starter",
        base_settings="sf_owner_settings",
        extra_pythonpath=(str(mod),),
    )
    assert res["stages"]["D_evaluation"] == "PASS"
    assert {p.name: _sha(p) for p in owner.iterdir()} == before


def test_settings_without_the_override_are_refused_before_anything_runs(tmp_path, monkeypatch):
    def unsafe(run_root, base_settings):  # a module that keeps the configured databases
        (run_root / "settings").mkdir(parents=True)
        (run_root / "settings" / f"{demo.SETTINGS_NAME}.py").write_text(
            f"from {base_settings} import *  # noqa\n"
        )
        return demo.SETTINGS_NAME

    monkeypatch.setattr(demo, "_write_settings", unsafe)
    with pytest.raises(Exception, match="outside .*run; refusing"):
        demo.run_demo(out=tmp_path / "out", seed=1)
    assert not (tmp_path / "out/demo_results.json").exists()
    assert sorted(p.name for p in (tmp_path / "out/run/logs").iterdir()) == ["settings_probe.log"]


@pytest.mark.django_db(databases=["default", "ground_truth"])
def test_the_child_refuses_settings_that_are_not_the_demo_settings(tmp_path):
    with pytest.raises(CommandError, match="not demo_workflow's isolated settings"):
        call_command("demo_workflow", "--child", "--out", str(tmp_path / "x"))
    assert not (tmp_path / "x").exists()
    with override_settings(SF_DEMO_ISOLATED=True, SF_DEMO_RUN_DIR=str(tmp_path)):
        with pytest.raises(demo.DemoError, match="outside"):
            demo.assert_isolated()
    from django.db import connections

    test_db_dir = Path(str(connections.databases["default"]["NAME"])).parent
    with override_settings(SF_DEMO_ISOLATED=True, SF_DEMO_RUN_DIR=str(test_db_dir)):
        with pytest.raises(demo.DemoError, match="already exists"):
            demo.assert_isolated()


def test_a_non_empty_output_directory_is_refused(tmp_path):
    (tmp_path / "mine.txt").write_text("keep")
    with pytest.raises(demo.DemoError, match="not an empty directory"):
        demo.run_demo(out=tmp_path, seed=1)
    assert [p.name for p in tmp_path.iterdir()] == ["mine.txt"]
    with pytest.raises(CommandError, match="not an empty directory"):
        call_command("demo_workflow", "--out", str(tmp_path))


def test_a_failing_stage_exits_non_zero_and_is_reported(tmp_path):
    raw = load_raw()
    raw["as_of_date"] = "2026-06-30"  # violates a frozen rule (D7): generation refuses it
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump(raw))
    out = tmp_path / "out"
    with pytest.raises(demo.DemoError, match=r"failed \(exit 1\)"):
        demo.run_demo(out=out, seed=20261022, config=str(bad))
    res = json.loads((out / "demo_results.json").read_text())
    assert res["stages"]["A_dataset"] == "FAIL" and res["stages"]["D_evaluation"] == "NOT RUN"
    assert "invalid generation config" in res["error"]
    assert "| A_dataset | FAIL |" in (out / "demo_report.md").read_text()
    assert (out / "run/db").is_dir()  # kept for inspection


@pytest.mark.skipif(
    __import__("os").environ.get("SF_FULLSCALE") != "1",
    reason="full scale is opt-in: SF_FULLSCALE=1",
)
def test_full_scale_demo_gives_the_documented_ids(tmp_path):
    """ARCHITECTURE §9.3 documents the IDs on the full dataset (10,000 students, 30,000
    enrollments, 50,000 payments): STU-010001, ENR-2026-030001, INV-STU-010001-2026-000001,
    PAY-2026-050001, ALLOC-PAY-2026-050001-01. Sequential; long-running (runtime and memory are
    recorded when it is run)."""
    res = demo.run_demo(out=tmp_path / "full", seed=20261022, scale="full")
    c = res["E_api"]
    assert (c["student"], c["enrollment"], c["invoice"], c["payment"]) == (
        "STU-010001",
        "ENR-2026-030001",
        "INV-STU-010001-2026-000001",
        "PAY-2026-050001",
    )
    assert c["allocations"][0] == "ALLOC-PAY-2026-050001-01"
    for v in res["D_evaluation"]["by_type"].values():
        assert (v["tp"], v["fp"], v["fn"]) == (150, 0, 0)
    print(
        f"full-scale demo: {res['runtime_seconds']} s, peak {res['peak_rss_mb']} MB, "
        f"stages {res['timings']}"
    )
