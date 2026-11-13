"""V-ISO: normal API endpoints never read from or write to the ground_truth database.

Every Phase 1 and Phase 2 operation is called on success *and* error paths (400, 401, 403, 404, 405,
409, 422) while every query on every non-default connection is captured.
"""

from contextlib import ExitStack
from datetime import date

import pytest
from django.db import DEFAULT_DB_ALIAS, connections
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

pytestmark = pytest.mark.django_db(databases="__all__")


def _calls(admin, staff, anon, sid):
    yield admin.get("/api/v1/students/")
    yield admin.get("/api/v1/students/", {"status": "BAD"})  # 400
    yield admin.post(
        "/api/v1/students/",
        {
            "first_name": "A",
            "last_name": "B",
            "email": "ab@example.edu",
            "date_of_birth": "2006-01-01",
            "residency": "DOMESTIC",
            "program": "PRG-001",
            "admit_term": "TERM-2026-SPRING",
            "gpa": "3.00",
            "need_index": "0.10",
        },
        format="json",
    )
    yield admin.post("/api/v1/students/", {"gpa": 3.0}, format="json")  # 400
    yield admin.get(f"/api/v1/students/{sid}/")
    yield admin.get("/api/v1/students/STU-999999/")  # 404
    yield admin.patch(f"/api/v1/students/{sid}/", {"last_name": "Z"}, format="json")
    yield admin.put(f"/api/v1/students/{sid}/", {}, format="json")  # 405
    yield admin.post(
        f"/api/v1/students/{sid}/status/", {"to_status": "GRADUATED", "reason": "X"}, format="json"
    )
    yield admin.post(
        f"/api/v1/students/{sid}/status/", {"to_status": "ACTIVE", "reason": "X"}, format="json"
    )  # 409
    yield admin.get("/api/v1/programs/")
    yield admin.post(
        "/api/v1/programs/",
        {
            "code": "Q",
            "name": "Q",
            "level": "UG",
            "department": "D",
            "is_stem": False,
            "has_lab": False,
            "min_credits": 6,
            "max_credits": 8,
        },
        format="json",
    )
    yield admin.get("/api/v1/programs/PRG-001/")
    yield admin.get("/api/v1/terms/")
    yield admin.get("/api/v1/terms/TERM-2026-FALL/")
    yield admin.post(
        "/api/v1/terms/",
        {
            "year": 2027,
            "season": "SPRING",
            "is_billable": False,
            "invoice_date": "2027-01-04",
            "due_date": "2027-02-01",
            "start_date": "2027-01-25",
            "census_date": "2027-02-22",
            "end_date": "2027-05-08",
        },
        format="json",
    )
    yield admin.post(
        "/api/v1/enrollments/",
        {
            "student": "STU-000002",
            "term": "TERM-2026-FALL",
            "credits": 16,
            "enrolled_on": "2026-08-20",
        },
        format="json",
    )
    yield admin.post(
        "/api/v1/enrollments/",
        {
            "student": "STU-000002",
            "term": "TERM-2026-FALL",
            "credits": 16,
            "enrolled_on": "2026-08-20",
        },
        format="json",
    )  # 409
    yield admin.post(
        "/api/v1/enrollments/",
        {
            "student": "STU-000002",
            "term": "TERM-2025-FALL",
            "credits": 16,
            "enrolled_on": "2025-09-01",
        },
        format="json",
    )  # 422
    yield admin.get("/api/v1/enrollments/")
    yield admin.get("/api/v1/enrollments/ENR-2026-000001/")
    yield admin.get("/api/v1/enrollments/ENR-2026-000001/events/")
    yield staff.post("/api/v1/programs/", {}, format="json")  # 403
    yield anon.get("/api/v1/students/")  # 401
    yield admin.get("/api/v1/nothing-here/")  # 404 catch-all


def test_no_api_operation_touches_ground_truth(admin_api, staff_api, student):
    others = [a for a in connections if a != DEFAULT_DB_ALIAS]
    assert others == ["ground_truth"]
    with ExitStack() as stack:
        captured = {a: stack.enter_context(CaptureQueriesContext(connections[a])) for a in others}
        default = stack.enter_context(CaptureQueriesContext(connections[DEFAULT_DB_ALIAS]))
        statuses = [
            r.status_code for r in _calls(admin_api, staff_api, APIClient(), student.student_id)
        ]
    assert statuses == [
        200,
        400,
        201,
        400,
        200,
        404,
        200,
        405,
        200,
        409,
        200,
        201,
        200,
        200,
        200,
        201,
        201,
        409,
        422,
        200,
        200,
        200,
        403,
        401,
        404,
    ]
    writes = [
        q["sql"]
        for q in default.captured_queries
        if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE"))
    ]
    assert writes, "the calls really wrote to the default database"
    assert {a: ctx.captured_queries for a, ctx in captured.items()} == {"ground_truth": []}


def test_ground_truth_database_holds_no_domain_tables():
    tables = set(connections["ground_truth"].introspection.table_names())
    assert tables == {"django_migrations"}
    domain = {
        "students_student",
        "academics_enrollment",
        "academics_program",
        "academics_term",
        "core_idsequence",
        "authtoken_token",
        "auth_user",
    }
    assert domain <= set(connections["default"].introspection.table_names())


def test_router_sends_every_phase1_model_to_default():
    from django.apps import apps
    from django.db import router

    for model in apps.get_models():
        assert router.db_for_read(model) == "default", model
        assert router.db_for_write(model) == "default", model


# --- Phase 2: billing ----------------------------------------------------------------------------

FEE = "FEE-PRG-001-2026-FALL-TUITION-DOM-V1"
INV = "INV-STU-000001-2026-000001"


def _billing_calls(finance, staff, anon):
    yield finance.post(
        "/api/v1/fee-schedules/",
        {
            "program": "PRG-001",
            "term": "TERM-2026-FALL",
            "fee_type": "TUITION",
            "residency": "DOMESTIC",
            "charge_basis": "PER_CREDIT",
            "amount": "9000.00",
            "effective_from": "2026-01-01",
        },
        format="json",
    )
    yield finance.post(
        "/api/v1/fee-schedules/",
        {
            "program": "PRG-001",
            "term": "TERM-2026-FALL",
            "fee_type": "REGISTRATION",
            "residency": "ALL",
            "charge_basis": "FLAT",
            "amount": "5000.00",
            "effective_from": "2026-01-01",
        },
        format="json",
    )
    yield finance.post(
        "/api/v1/fee-schedules/",
        {
            "program": "PRG-001",
            "term": "TERM-2026-FALL",
            "fee_type": "LAB",
            "residency": "ALL",
            "charge_basis": "FLAT",
            "amount": "6000.00",
            "effective_from": "2026-01-01",
        },
        format="json",
    )
    yield finance.post("/api/v1/fee-schedules/", {"fee_type": "X"}, format="json")  # 400
    yield finance.post(
        f"/api/v1/fee-schedules/{FEE}/new-version/",
        {"amount": "9360.00", "effective_from": "2026-01-01"},
        format="json",
    )  # 422
    yield finance.post(
        f"/api/v1/fee-schedules/{FEE}/new-version/",
        {"amount": "9360.00", "effective_from": "2026-08-01"},
        format="json",
    )
    yield finance.get("/api/v1/fee-schedules/", {"in_force_on": "2026-08-17"})
    yield finance.get(f"/api/v1/fee-schedules/{FEE}/history/")
    yield finance.get("/api/v1/fee-schedules/NOPE/history/")  # 404
    yield finance.post(
        "/api/v1/invoices/generate/", {"enrollment_id": "ENR-2026-000001"}, format="json"
    )
    yield finance.post(
        "/api/v1/invoices/generate/", {"enrollment_id": "ENR-2026-000001"}, format="json"
    )  # 409
    yield finance.get("/api/v1/invoices/")
    yield finance.get(f"/api/v1/invoices/{INV}/")
    yield finance.post(
        "/api/v1/billing/late-fees/assess/",
        {"as_of_date": "2026-09-29", "dry_run": True},
        format="json",
    )
    yield finance.post(
        "/api/v1/billing/late-fees/assess/", {"as_of_date": "2026-09-29"}, format="json"
    )
    yield finance.post(
        "/api/v1/billing/collection-status/refresh/", {"as_of_date": "2026-10-01"}, format="json"
    )
    yield finance.get("/api/v1/students/STU-000001/ledger/")
    yield finance.get("/api/v1/students/STU-000001/statement/")
    yield finance.get("/api/v1/students/STU-000001/balance/", {"as_of": "2026-12-31"})
    yield finance.post(f"/api/v1/invoices/{INV}/void/", {"void_date": "2026-10-02"}, format="json")
    yield finance.post(
        f"/api/v1/invoices/{INV}/void/", {"void_date": "2026-10-03"}, format="json"
    )  # 409
    yield finance.put(f"/api/v1/invoices/{INV}/", {}, format="json")  # 405
    yield staff.post("/api/v1/invoices/generate/", {}, format="json")  # 403
    yield anon.get("/api/v1/invoices/")  # 401


def test_no_billing_operation_touches_ground_truth(api_client_for, staff_api, student, terms):
    from academics import services

    services.enroll(
        student=student,
        term=terms["TERM-2026-FALL"],
        credits=16,
        enrolled_on=date(2026, 8, 10),
        actor="t",
    )
    finance = api_client_for("platform_admin")
    others = [a for a in connections if a != DEFAULT_DB_ALIAS]
    with ExitStack() as stack:
        captured = {a: stack.enter_context(CaptureQueriesContext(connections[a])) for a in others}
        default = stack.enter_context(CaptureQueriesContext(connections[DEFAULT_DB_ALIAS]))
        statuses = [r.status_code for r in _billing_calls(finance, staff_api, APIClient())]
    assert statuses == [
        201, 201, 201, 400, 422, 201, 200, 200, 404,  # fee schedules
        201, 409, 200, 200,  # invoices
        200, 200, 200,  # late fees (dry run, real), refresh
        200, 200, 200,  # ledger, statement, balance
        200, 409, 405, 403, 401,  # void, void again, PUT, staff write, anonymous
    ]  # fmt: skip
    writes = [
        q["sql"]
        for q in default.captured_queries
        if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE"))
    ]
    assert any("billing_ledgerentry" in w for w in writes)
    assert {a: ctx.captured_queries for a, ctx in captured.items()} == {"ground_truth": []}


def test_ground_truth_database_holds_no_billing_tables():
    assert not [
        t
        for t in connections["ground_truth"].introspection.table_names()
        if t.startswith("billing_")
    ]
    billing = {
        "billing_feeschedule",
        "billing_invoice",
        "billing_invoiceline",
        "billing_invoiceevent",
        "billing_ledgerentry",
    }
    assert billing <= set(connections["default"].introspection.table_names())


def test_router_refuses_billing_migrations_on_ground_truth():
    from django.apps import apps
    from django.db import router

    for model in apps.get_app_config("billing").get_models():
        assert (
            router.allow_migrate("ground_truth", "billing", model_name=model._meta.model_name)
            is False
        )
        assert router.allow_migrate("default", "billing", model_name=model._meta.model_name) is True
