"""The generate_data / evaluate commands (ARCHITECTURE §11.1) and the admin-only datagen API
(§9.2, §13): platform_admin only, starter-scale synchronous generation, run detail without
labels, on-demand evaluation of a COMPLETED ALL-scope run."""

import json
from datetime import date

import pytest
from django.core.management import CommandError, call_command
from rest_framework.test import APIClient

from core.permissions import ALL_ROLES
from datagen.models import GenerationRun, GroundTruthLabel
from reconciliation import services as reconciliation

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "ground_truth"])

RUNS = "/api/v1/datagen/runs/"


def test_generate_data_command_end_to_end(capsys):
    call_command("generate_data", "--seed", "20261022", "--scale", "starter")
    out = capsys.readouterr().out
    assert "GEN-2026-0001 seed=20261022 scale=starter (0.0100)" in out
    assert "labels=15" in out
    for t in (
        "PAYMENT_OVER_ALLOCATED",
        "SCHOLARSHIP_EXCEEDS_ELIGIBLE_FEES",
        "DUPLICATE_INVOICE",
        "REFUND_EXCEEDS_NET_PAID",
        "BALANCE_LEDGER_MISMATCH",
    ):
        assert f"{t}: TP=3 FP=0 FN=0 P=1.0000 R=1.0000 F1=1.0000" in out
    assert "consequential=3 unexpected=0 non_anomalous_reconciled=100.00% meets_targets=True" in out
    with pytest.raises(CommandError, match="Generation needs fresh, migrated databases"):
        call_command("generate_data")


def test_generate_data_rejects_a_bad_scale_factor():
    with pytest.raises(CommandError, match="scale factor must be in"):
        call_command("generate_data", "--scale-factor", "1.5")


def test_evaluate_command_writes_the_report(tmp_path, capsys):
    call_command("generate_data", "--skip-reconciliation")
    with pytest.raises(CommandError, match="No COMPLETED ALL-scope reconciliation run"):
        call_command("evaluate", "--out", str(tmp_path / "r.json"))
    run = reconciliation.run_reconciliation(
        scope="ALL", as_of_date=date(2026, 12, 31), triggered_by="COMMAND"
    )
    call_command("evaluate", "--reconciliation-run", run.pk, "--out", str(tmp_path / "r.json"))
    report = json.loads((tmp_path / "r.json").read_text())
    assert report["meets_targets"] is True and report["reconciliation_run"] == run.pk
    assert report["generation_run"] == "GEN-2026-0001"
    assert "wrote" in capsys.readouterr().out


@pytest.fixture
def clients(api_client_for):
    return {role: api_client_for(role) for role in ALL_ROLES}


def test_only_platform_admin_may_use_the_datagen_api(clients):
    assert APIClient().get(RUNS).status_code == 401
    for role, client in clients.items():
        expected = 200 if role == "platform_admin" else 403
        assert client.get(RUNS).status_code == expected, role
        if role != "platform_admin":
            assert client.post(RUNS, {}, format="json").status_code == 403, role
    assert GenerationRun.objects.count() == 0


def test_api_generates_lists_and_evaluates(clients):
    admin = clients["platform_admin"]
    r = admin.post(RUNS, {"scale": "starter"}, format="json")
    assert r.status_code == 201, r.content
    body = r.json()
    assert body["generation_run"]["run_id"] == "GEN-2026-0001"
    assert body["evaluation"]["meets_targets"] is True
    assert "labels" not in json.dumps(body["generation_run"])  # labels are never returned
    recon = body["reconciliation_run"]

    assert [x["run_id"] for x in admin.get(RUNS).json()["results"]] == ["GEN-2026-0001"]
    detail = admin.get(f"{RUNS}GEN-2026-0001/").json()
    assert detail["checksums"]["dataset"] == body["generation_run"]["checksums"]["dataset"]

    ev = admin.get(f"{RUNS}GEN-2026-0001/evaluation/", {"reconciliation_run": recon}).json()
    assert ev == body["evaluation"]
    assert ev["by_type"]["DUPLICATE_INVOICE"]["precision"] == "1.0000"

    again = admin.post(RUNS, {"scale": "starter"}, format="json")
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "DATASET_NOT_FRESH"
    assert GroundTruthLabel.objects.using("ground_truth").count() == 15


def test_evaluation_refuses_student_scope_runs(clients, monkeypatch):
    admin = clients["platform_admin"]
    admin.post(RUNS, {"scale": "starter"}, format="json")
    from students.models import Student

    run = reconciliation.run_reconciliation(
        scope="STUDENT",
        student=Student.objects.order_by("pk").first(),
        as_of_date=date(2026, 12, 31),
        triggered_by="API",
    )
    r = admin.get(f"{RUNS}GEN-2026-0001/evaluation/", {"reconciliation_run": run.pk})
    assert r.status_code == 400
    assert "only an ALL-scope run" in r.json()["error"]["details"]["reconciliation_run"][0]


def test_full_scale_is_not_available_through_the_api(clients):
    r = clients["platform_admin"].post(RUNS, {"scale": "full"}, format="json")
    assert r.status_code == 400
    assert GenerationRun.objects.count() == 0
