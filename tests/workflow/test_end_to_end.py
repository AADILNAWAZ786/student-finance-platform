"""ARCHITECTURE §9.3 demonstration workflow, steps 1–3b, entirely through the API.

Later steps (4 scholarships … 11 anomalies) are added by the phase that builds them. The
documented IDs (STU-010001, ENR-2026-030001) assume the generated dataset is loaded; on an
empty database the same calls produce the first IDs (STU-000001, ENR-2026-000001).
"""

from datetime import date

import pytest

pytestmark = pytest.mark.django_db

FALL = {
    "year": 2026,
    "season": "FALL",
    "is_billable": True,
    "invoice_date": "2026-08-17",
    "due_date": "2026-09-14",
    "start_date": "2026-09-07",
    "census_date": "2026-10-05",
    "end_date": "2026-12-19",
}


def _ok(r, status):
    assert r.status_code == status, r.content
    return r.json()


@pytest.fixture
def api(api_client_for, frozen_today):
    frozen_today(date(2026, 8, 1))  # business date for the student's first status event
    return api_client_for("platform_admin")


@pytest.fixture
def master_data(api):
    program = _ok(
        api.post(
            "/api/v1/programs/",
            {
                "code": "BSC-CS",
                "name": "BSc Computer Science",
                "level": "UG",
                "department": "Computing",
                "is_stem": True,
                "has_lab": True,
                "min_credits": 12,
                "max_credits": 20,
            },
            format="json",
        ),
        201,
    )
    _ok(api.post("/api/v1/terms/", FALL, format="json"), 201)
    for fee_type, residency, basis, amount in [
        ("TUITION", "DOMESTIC", "PER_CREDIT", "9000.00"),
        ("LAB", "ALL", "FLAT", "6000.00"),
        ("REGISTRATION", "ALL", "FLAT", "5000.00"),
        ("TECHNOLOGY", "ALL", "FLAT", "4000.00"),
    ]:
        _ok(
            api.post(
                "/api/v1/fee-schedules/",
                {
                    "program": program["program_id"],
                    "term": "TERM-2026-FALL",
                    "fee_type": fee_type,
                    "residency": residency,
                    "charge_basis": basis,
                    "amount": amount,
                    "effective_from": "2026-01-01",
                },
                format="json",
            ),
            201,
        )
    return program


def test_workflow_steps_1_to_3b(api, master_data):
    # 1. create a student
    student = _ok(
        api.post(
            "/api/v1/students/",
            {
                "first_name": "Asha",
                "last_name": "Rao",
                "email": "asha.rao@example.edu",
                "date_of_birth": "2006-03-14",
                "residency": "DOMESTIC",
                "program": master_data["program_id"],
                "admit_term": "TERM-2026-FALL",
                "gpa": "3.72",
                "need_index": "0.35",
            },
            format="json",
        ),
        201,
    )
    sid = student["student_id"]
    assert sid == "STU-000001"

    # 2. enroll in TERM-2026-FALL, 16 credits
    enrollment = _ok(
        api.post(
            "/api/v1/enrollments/",
            {"student": sid, "term": "TERM-2026-FALL", "credits": 16, "enrolled_on": "2026-08-10"},
            format="json",
        ),
        201,
    )
    assert (enrollment["enrollment_id"], enrollment["status"]) == ("ENR-2026-000001", "ENROLLED")

    # 3. generate the invoice: lines, CHARGE entries, ISSUED / UNPAID
    invoice = _ok(
        api.post(
            "/api/v1/invoices/generate/",
            {"enrollment_id": enrollment["enrollment_id"]},
            format="json",
        ),
        201,
    )
    assert invoice["invoice_id"] == "INV-STU-000001-2026-000001"
    assert (invoice["lifecycle_status"], invoice["collection_status"]) == ("ISSUED", "UNPAID")
    assert [line["fee_type"] for line in invoice["lines"]] == [
        "TUITION",
        "LAB",
        "REGISTRATION",
        "TECHNOLOGY",
    ]
    assert invoice["total_amount"] == invoice["amounts"]["outstanding"] == "159000.00"

    ledger = _ok(api.get(f"/api/v1/students/{sid}/ledger/"), 200)
    assert [(e["entry_type"], e["invoice_line"]) for e in ledger["results"]] == [
        ("CHARGE", line["line_id"]) for line in invoice["lines"]
    ]
    balance = _ok(api.get(f"/api/v1/students/{sid}/balance/"), 200)
    assert balance["ledger_balance"] == "159000.00" and balance["identity"]["holds"] is True

    # 3b. the same call again is rejected, and nothing new is written
    r = api.post(
        "/api/v1/invoices/generate/", {"enrollment_id": enrollment["enrollment_id"]}, format="json"
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "DUPLICATE_INVOICE"
    assert r.json()["error"]["details"]["existing_invoice_id"] == invoice["invoice_id"]
    assert _ok(api.get("/api/v1/invoices/"), 200)["count"] == 1
    assert _ok(api.get(f"/api/v1/students/{sid}/ledger/"), 200)["count"] == 4
