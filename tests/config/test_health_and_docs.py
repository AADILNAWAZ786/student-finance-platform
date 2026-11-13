"""Health endpoint and API documentation (ARCHITECTURE.md §9.2)."""

import json
from unittest import mock

import pytest
from django.core.management import call_command
from django.db import DatabaseError
from rest_framework.test import APIClient

pytestmark = [pytest.mark.phase0, pytest.mark.django_db(databases=["default", "ground_truth"])]


@pytest.fixture
def client():
    return APIClient()


def test_health_ok_without_authentication(client):
    r = client.get("/api/v1/health/")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["api_version"] == "v1"
    assert body["django_version"].startswith("5.2.")
    assert set(body["databases"]) == {"default", "ground_truth"}
    assert body["databases"]["default"] == {
        "status": "ok",
        "journal_mode": "wal",
        "foreign_keys": True,
        "transaction_mode": "IMMEDIATE",
    }
    assert body["databases"]["ground_truth"]["status"] == "ok"


def test_health_reports_degraded_with_503(client):
    with mock.patch(
        "config.health._probe",
        side_effect=lambda alias: (
            {
                "status": "error",
                "journal_mode": None,
                "foreign_keys": None,
                "transaction_mode": None,
                "error": "disk I/O error",
            }
            if alias == "ground_truth"
            else {
                "status": "ok",
                "journal_mode": "wal",
                "foreign_keys": True,
                "transaction_mode": "IMMEDIATE",
            }
        ),
    ):
        r = client.get("/api/v1/health/")
    assert r.status_code == 503
    assert r.json()["status"] == "degraded"


def test_probe_turns_database_errors_into_error_status():
    from config import health as views

    with mock.patch.object(
        views.connections["default"],
        "cursor",
        side_effect=DatabaseError("unable to open database file"),
    ):
        result = views._probe("default")
    assert result["status"] == "error" and "unable to open" in result["error"]


def test_health_rejects_writes_with_envelope(client):
    r = client.post("/api/v1/health/", {}, format="json")
    assert r.status_code == 405
    assert r.json()["error"]["code"] == "METHOD_NOT_ALLOWED"


def test_openapi_schema_lists_health(client):
    r = client.get("/api/schema/?format=json")
    assert r.status_code == 200
    schema = json.loads(r.content)
    assert schema["openapi"].startswith("3.")
    assert schema["info"]["title"] == "Student Finance API"
    op = schema["paths"]["/api/v1/health/"]["get"]
    assert op["operationId"] == "health_check"
    assert {"200", "503"} <= set(op["responses"])


@pytest.mark.parametrize("path", ["/api/docs/", "/api/redoc/"])
def test_docs_pages_render(client, path):
    r = client.get(path)
    assert r.status_code == 200
    assert b"/api/schema/" in r.content


def test_schema_generation_has_no_warnings(tmp_path):
    out = tmp_path / "schema.yaml"
    call_command("spectacular", "--validate", "--fail-on-warn", "--file", str(out))
    assert "/api/v1/health/" in out.read_text()


def test_no_missing_migrations():
    call_command("makemigrations", "--check", "--dry-run", verbosity=0)


def _schema(client):
    return json.loads(client.get("/api/schema/?format=json").content)


def test_health_docs_show_both_databases_and_503(client):
    """Phase 1 fix: examples document both configured databases and the 503 response."""
    from django.conf import settings

    responses = _schema(client)["paths"]["/api/v1/health/"]["get"]["responses"]
    ok = next(iter(responses["200"]["content"]["application/json"]["examples"].values()))
    bad = next(iter(responses["503"]["content"]["application/json"]["examples"].values()))
    assert set(ok["value"]["databases"]) == set(settings.DATABASES)
    assert set(bad["value"]["databases"]) == set(settings.DATABASES)
    assert ok["value"]["status"] == "ok" and bad["value"]["status"] == "degraded"
    assert any(
        db["status"] == "error" and "error" in db for db in bad["value"]["databases"].values()
    )
    # examples have exactly the keys the live endpoint returns
    live = client.get("/api/v1/health/").json()
    assert set(ok["value"]) == set(live)
    for alias, db in live["databases"].items():
        assert set(ok["value"]["databases"][alias]) == set(db)
        assert ok["value"]["databases"][alias]["journal_mode"] == db["journal_mode"]


def test_phase1_endpoints_are_documented(client):
    schema = _schema(client)
    ops = {op["operationId"] for path in schema["paths"].values() for op in path.values()}
    assert {
        "students_list",
        "students_create",
        "students_retrieve",
        "students_partial_update",
        "students_status_create",
        "programs_list",
        "programs_create",
        "programs_retrieve",
        "terms_list",
        "terms_create",
        "terms_retrieve",
        "enrollments_list",
        "enrollments_create",
        "enrollments_retrieve",
        "enrollments_events_list",
    } <= ops
    create = schema["paths"]["/api/v1/enrollments/"]["post"]
    assert {"201", "400", "401", "403", "409", "422"} <= set(create["responses"])
    params = {p["name"] for p in schema["paths"]["/api/v1/students/"]["get"]["parameters"]}
    assert {"program", "status", "residency", "page", "page_size", "ordering"} <= params
