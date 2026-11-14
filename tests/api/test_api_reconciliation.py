"""Reconciliation API (ARCHITECTURE §9.2; D-R6, D-R10, D-R11): every endpoint validated
against the served OpenAPI document, the §13 role matrix, strict input, 405 on edits,
filters, and the error envelope."""

import pytest

from reconciliation.models import AnomalyFinding, BalanceSnapshot, ReconciliationRun
from tests.reconciliation_fixtures import inject_payment_over_allocated

pytestmark = pytest.mark.django_db

RUNS, ANOMALIES, SNAPS = (
    "/api/v1/reconciliation/runs/",
    "/api/v1/anomalies/",
    "/api/v1/balance-snapshots/",
)


@pytest.fixture
def admin(api_client_for):
    return api_client_for("platform_admin")


def _counts():
    return (
        ReconciliationRun.objects.count(),
        AnomalyFinding.objects.count(),
        BalanceSnapshot.objects.count(),
    )


def test_run_all_then_read_the_run_and_its_findings(admin, clean_world, assert_matches_openapi):
    payment_id = inject_payment_over_allocated(clean_world["S16"])
    r = admin.post(RUNS, {"scope": "ALL", "as_of_date": "2026-12-31"}, format="json")
    assert r.status_code == 201, r.content
    assert_matches_openapi(r, "/api/v1/reconciliation/runs/", "post")
    run = r.json()
    assert (run["run_id"], run["status"], run["triggered_by"], run["findings_count"]) == (
        "RECON-2026-000001",
        "COMPLETED",
        "API",
        1,
    )
    assert run["summary"]["findings_by_issue_type"]["PAYMENT_OVER_ALLOCATED"] == 1
    assert run["summary"]["students"]["pct_reconciled"] == "100.00"

    r = admin.get(f"{RUNS}{run['run_id']}/")
    assert_matches_openapi(r, "/api/v1/reconciliation/runs/{run_id}/", "get")
    assert r.json() == run
    r = admin.get(RUNS)
    assert_matches_openapi(r, "/api/v1/reconciliation/runs/", "get")
    assert [x["run_id"] for x in r.json()["results"]] == [run["run_id"]]

    r = admin.get(ANOMALIES, {"run": run["run_id"]})
    assert_matches_openapi(r, "/api/v1/anomalies/", "get")
    [f] = r.json()["results"]
    assert {
        k: f[k]
        for k in (
            "issue_type",
            "rule_id",
            "severity",
            "record_id",
            "student",
            "recommended_action",
            "expected_value",
            "actual_value",
        )
    } == {
        "issue_type": "PAYMENT_OVER_ALLOCATED",
        "rule_id": "BR-ALC-01",
        "severity": "CRITICAL",
        "record_id": payment_id,
        "student": clean_world["S16"].pk,
        "recommended_action": "REJECT_ALLOCATION",
        "expected_value": "50000.00",
        "actual_value": "51000.00",
    }
    r = admin.get(f"{ANOMALIES}{f['finding_id']}/")
    assert_matches_openapi(r, "/api/v1/anomalies/{finding_id}/", "get")
    assert r.json() == f


def test_anomaly_filters(admin, clean_world):
    inject_payment_over_allocated(clean_world["S16"])
    run = admin.post(RUNS, {"scope": "ALL", "as_of_date": "2026-12-31"}, format="json").json()
    for params, n in (
        ({"issue_type": "PAYMENT_OVER_ALLOCATED"}, 1),
        ({"issue_type": "DUPLICATE_INVOICE"}, 0),
        ({"severity": "CRITICAL"}, 1),
        ({"severity": "LOW"}, 0),
        ({"student": clean_world["S16"].pk}, 1),
        ({"student": clean_world["S1"].pk}, 0),
        ({"run": run["run_id"]}, 1),
        ({"run": "RECON-2026-999999"}, 0),
    ):
        assert admin.get(ANOMALIES, params).json()["count"] == n, params


def test_student_scope_run_lists_skipped_global_checks(admin, clean_world):
    sid = clean_world["S1"].pk
    r = admin.post(
        RUNS, {"scope": "STUDENT", "student_id": sid, "as_of_date": "2026-12-31"}, format="json"
    )
    assert r.status_code == 201
    body = r.json()
    assert (body["scope"], body["student"], body["findings_count"]) == ("STUDENT", sid, 0)
    assert {x["check_id"] for x in body["summary"]["skipped_checks"]} == {
        "RL-BILLABLE-TERM-MASTER",
        "S-DUP-MASTER",
        "S-ENUM-MASTER",
        "S-FK-ORPHAN",
        "S-ID-GAPS-YEAR",
        "S-ID-MASTER",
        "T-FEE-CHAIN",
        "T-TERM-CENSUS",
    }


def test_snapshots_one_all_and_existing(admin, clean_world, assert_matches_openapi):
    sid = clean_world["S1"].pk
    r = admin.post(SNAPS, {"student_id": sid, "as_of_date": "2026-12-19"}, format="json")
    assert r.status_code == 201, r.content
    assert_matches_openapi(r, "/api/v1/balance-snapshots/", "post")
    one = r.json()
    assert (one["created_count"], one["existing_count"], one["student_id"]) == (1, 0, sid)
    snap = one["snapshots"][0]
    assert (snap["snapshot_id"], snap["created"]) == ("SNAP-2026-000001", True)
    # D-R6: the same request again returns it unchanged, created=false, 200
    r = admin.post(SNAPS, {"student_id": sid, "as_of_date": "2026-12-19"}, format="json")
    assert r.status_code == 200
    assert r.json()["snapshots"] == [{**snap, "created": False}]
    # every student: the existing one is reported, the others created
    r = admin.post(SNAPS, {"as_of_date": "2026-12-19"}, format="json")
    body = r.json()
    assert (r.status_code, body["created_count"], body["existing_count"]) == (
        201,
        len(clean_world) - 1,
        1,
    )
    r = admin.get(SNAPS, {"student": sid})
    assert_matches_openapi(r, "/api/v1/balance-snapshots/", "get")
    assert [x["snapshot_id"] for x in r.json()["results"]] == [snap["snapshot_id"]]
    r = admin.get(f"{SNAPS}{snap['snapshot_id']}/")
    assert_matches_openapi(r, "/api/v1/balance-snapshots/{snapshot_id}/", "get")
    assert r.json() == {k: v for k, v in snap.items() if k != "created"}


@pytest.mark.parametrize("role", ["learner", "instructor", "academic_admin"])
def test_other_roles_get_403_everywhere_and_nothing_is_written(api_client_for, clean_world, role):
    api = api_client_for(role)
    before = _counts()
    for method, path, body in (
        ("post", RUNS, {"scope": "ALL", "as_of_date": "2026-12-31"}),
        ("post", SNAPS, {"as_of_date": "2026-12-31"}),
        ("get", RUNS, None),
        ("get", ANOMALIES, None),
        ("get", SNAPS, None),
    ):
        r = getattr(api, method)(path, body, format="json")
        assert r.status_code == 403, (role, method, path)
        assert r.json()["error"]["code"] == "PERMISSION_DENIED"
    assert _counts() == before


def test_support_staff_reads_but_cannot_run_or_snapshot(api_client_for, admin, clean_world):
    admin.post(RUNS, {"scope": "ALL", "as_of_date": "2026-12-31"}, format="json")
    staff = api_client_for("support_staff")
    for path in (RUNS, ANOMALIES, SNAPS, f"{RUNS}RECON-2026-000001/"):
        assert staff.get(path).status_code == 200, path
    before = _counts()
    assert (
        staff.post(RUNS, {"scope": "ALL", "as_of_date": "2026-12-31"}, format="json").status_code
        == 403
    )
    assert staff.post(SNAPS, {"as_of_date": "2026-12-31"}, format="json").status_code == 403
    assert _counts() == before


def test_unauthenticated_is_401(client, clean_world):
    for path in (RUNS, ANOMALIES, SNAPS):
        assert client.get(path).status_code == 401


@pytest.mark.parametrize(
    ("path", "body", "details"),
    [
        (
            RUNS,
            {"scope": "STUDENT", "as_of_date": "2026-12-31"},
            {"student_id": ["Required when scope is STUDENT."]},
        ),
        (
            RUNS,
            {"scope": "ALL", "student_id": "STU-000001", "as_of_date": "2026-12-31"},
            {"student_id": ["Must be omitted when scope is ALL."]},
        ),
        (
            RUNS,
            {"scope": "SOME", "as_of_date": "2026-12-31"},
            {"scope": ['"SOME" is not a valid choice.']},
        ),
        (RUNS, {"scope": "ALL"}, {"as_of_date": ["This field is required."]}),
        (
            RUNS,
            {"scope": "ALL", "as_of_date": "2027-01-01"},
            {"as_of_date": ["Must be a business date between 2026-01-01 and 2026-12-31."]},
        ),
        (
            RUNS,
            {"scope": "ALL", "as_of_date": "2026-12-31", "status": "COMPLETED"},
            {"status": ["Unknown field."]},
        ),
        (
            SNAPS,
            {"as_of_date": "2026-12-31", "reported_balance": "0.00"},
            {"reported_balance": ["Unknown field."]},
        ),
        (
            SNAPS,
            {"as_of_date": "2026-12-31", "student_id": "STU-999999"},
            {"student_id": ['Invalid pk "STU-999999" - object does not exist.']},
        ),
    ],
)
def test_strict_input(admin, clean_world, path, body, details):
    before = _counts()
    r = admin.post(path, body, format="json")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"
    assert r.json()["error"]["details"] == details
    assert _counts() == before


def test_records_are_read_and_action_only(admin, clean_world):
    inject_payment_over_allocated(clean_world["S16"])
    admin.post(RUNS, {"scope": "ALL", "as_of_date": "2026-12-31"}, format="json")
    admin.post(SNAPS, {"as_of_date": "2026-12-31"}, format="json")
    for path in (
        f"{RUNS}RECON-2026-000001/",
        f"{ANOMALIES}FND-2026-000001/",
        f"{SNAPS}SNAP-2026-000001/",
    ):
        for method in ("put", "patch", "delete"):
            r = getattr(admin, method)(path, {}, format="json")
            assert r.status_code == 405, (method, path)
            assert r.json()["error"]["code"] == "METHOD_NOT_ALLOWED"
    for method in ("put", "patch", "delete"):
        assert getattr(admin, method)(ANOMALIES, {}, format="json").status_code == 405


@pytest.mark.parametrize(
    "path",
    [f"{RUNS}RECON-2026-999999/", f"{ANOMALIES}FND-2026-999999/", f"{SNAPS}SNAP-2026-999999/"],
)
def test_unknown_ids_are_404(admin, clean_world, path):
    r = admin.get(path)
    assert (r.status_code, r.json()["error"]["code"]) == (404, "NOT_FOUND")


def test_failed_run_is_returned_201_with_its_error(admin, clean_world, monkeypatch):
    from reconciliation import engine

    def boom(ds):
        raise RuntimeError("check exploded")

    monkeypatch.setattr(engine, "run_checks", boom)
    r = admin.post(RUNS, {"scope": "ALL", "as_of_date": "2026-12-31"}, format="json")
    assert r.status_code == 201
    body = r.json()
    assert (body["status"], body["findings_count"]) == ("FAILED", 0)
    assert body["summary"] == {"error": {"type": "RuntimeError", "message": "check exploded"}}


def test_command_uses_the_same_engine(clean_world):
    import io

    from django.core.management import call_command

    inject_payment_over_allocated(clean_world["S16"])
    out = io.StringIO()
    call_command("run_reconciliation", "--as-of", "2026-12-31", stdout=out)
    run = ReconciliationRun.objects.get()
    assert (run.triggered_by, run.status) == ("COMMAND", "COMPLETED")
    assert out.getvalue().splitlines()[0].startswith("RECON-2026-000001 COMPLETED scope=ALL")
    assert "PAYMENT_OVER_ALLOCATED: 1" in out.getvalue()
    call_command(
        "run_reconciliation",
        "--scope",
        "STUDENT",
        "--student",
        clean_world["S1"].pk,
        "--as-of",
        "2026-12-31",
        stdout=io.StringIO(),
    )
    assert ReconciliationRun.objects.filter(scope="STUDENT", triggered_by="COMMAND").count() == 1


def test_command_errors(clean_world):
    import io

    from django.core.management import call_command
    from django.core.management.base import CommandError

    with pytest.raises(CommandError, match="Required when scope is STUDENT"):
        call_command(
            "run_reconciliation",
            "--scope",
            "STUDENT",
            "--as-of",
            "2026-12-31",
            stdout=io.StringIO(),
        )
    with pytest.raises(CommandError, match="Unknown student"):
        call_command(
            "run_reconciliation",
            "--scope",
            "STUDENT",
            "--student",
            "STU-999999",
            "--as-of",
            "2026-12-31",
            stdout=io.StringIO(),
        )
    assert ReconciliationRun.objects.count() == 0
