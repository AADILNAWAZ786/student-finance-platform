"""Requirement 8: application endpoints use the default database only.

Every Phase 1 and Phase 2 endpoint is exercised while queries on every non-default connection are
captured; none may be issued. This proves the API cannot read ground truth.
"""

from contextlib import ExitStack

import pytest
from django.db import DEFAULT_DB_ALIAS, connections
from django.test.utils import CaptureQueriesContext

pytestmark = pytest.mark.django_db(databases="__all__")


def test_no_query_reaches_any_non_default_database(admin_api, student):
    others = [a for a in connections if a != DEFAULT_DB_ALIAS]
    assert others, "expected a second configured database"
    with ExitStack() as stack:
        captured = {a: stack.enter_context(CaptureQueriesContext(connections[a])) for a in others}
        default = stack.enter_context(CaptureQueriesContext(connections[DEFAULT_DB_ALIAS]))
        sid = student.student_id
        calls = [
            admin_api.get("/api/v1/students/"),
            admin_api.get(f"/api/v1/students/{sid}/"),
            admin_api.patch(f"/api/v1/students/{sid}/", {"first_name": "A"}, format="json"),
            admin_api.post(
                f"/api/v1/students/{sid}/status/",
                {"to_status": "ON_HOLD", "reason": "X"},
                format="json",
            ),
            admin_api.post(
                f"/api/v1/students/{sid}/status/",
                {"to_status": "ACTIVE", "reason": "X"},
                format="json",
            ),
            admin_api.get("/api/v1/programs/"),
            admin_api.get("/api/v1/programs/PRG-001/"),
            admin_api.get("/api/v1/terms/"),
            admin_api.get("/api/v1/terms/TERM-2026-FALL/"),
            admin_api.post(
                "/api/v1/enrollments/",
                {
                    "student": sid,
                    "term": "TERM-2026-FALL",
                    "credits": 16,
                    "enrolled_on": "2026-08-20",
                },
                format="json",
            ),
            admin_api.get("/api/v1/enrollments/"),
            admin_api.get("/api/v1/enrollments/ENR-2026-000001/"),
            admin_api.get("/api/v1/enrollments/ENR-2026-000001/events/"),
        ]
    assert [c.status_code for c in calls] == [200] * 9 + [201] + [200] * 3
    assert len(default.captured_queries) > 0  # the work really happened on default
    assert {a: len(ctx.captured_queries) for a, ctx in captured.items()} == dict.fromkeys(others, 0)


def test_no_billing_query_reaches_any_non_default_database(api_client_for, student, terms, program):
    """Phase 2: the same proof for every billing endpoint."""
    from datetime import date

    from academics import services
    from tests.billing_fixtures import make_fees

    services.enroll(
        student=student,
        term=terms["TERM-2026-FALL"],
        credits=16,
        enrolled_on=date(2026, 8, 10),
        actor="t",
    )
    make_fees(program, terms["TERM-2026-FALL"])
    finance = api_client_for("platform_admin")
    inv, fee = "INV-STU-000001-2026-000001", "FEE-PRG-001-2026-FALL-TUITION-DOM-V1"
    others = [a for a in connections if a != DEFAULT_DB_ALIAS]
    with ExitStack() as stack:
        captured = {a: stack.enter_context(CaptureQueriesContext(connections[a])) for a in others}
        default = stack.enter_context(CaptureQueriesContext(connections[DEFAULT_DB_ALIAS]))
        calls = [
            finance.post(
                "/api/v1/invoices/generate/", {"enrollment_id": "ENR-2026-000001"}, format="json"
            ),
            finance.post(
                f"/api/v1/fee-schedules/{fee}/new-version/",
                {"amount": "9360.00", "effective_from": "2026-09-01"},
                format="json",
            ),
            finance.get("/api/v1/fee-schedules/"),
            finance.get(f"/api/v1/fee-schedules/{fee}/history/"),
            finance.get("/api/v1/invoices/"),
            finance.get(f"/api/v1/invoices/{inv}/"),
            finance.post(
                "/api/v1/billing/late-fees/assess/", {"as_of_date": "2026-09-29"}, format="json"
            ),
            finance.post(
                "/api/v1/billing/collection-status/refresh/",
                {"as_of_date": "2026-10-01"},
                format="json",
            ),
            finance.get("/api/v1/students/STU-000001/ledger/"),
            finance.get("/api/v1/students/STU-000001/statement/"),
            finance.get("/api/v1/students/STU-000001/balance/"),
            finance.post(
                f"/api/v1/invoices/{inv}/void/", {"void_date": "2026-10-02"}, format="json"
            ),
        ]
    assert [c.status_code for c in calls] == [201, 201] + [200] * 10
    assert len(default.captured_queries) > 0
    assert {a: len(ctx.captured_queries) for a, ctx in captured.items()} == dict.fromkeys(others, 0)
