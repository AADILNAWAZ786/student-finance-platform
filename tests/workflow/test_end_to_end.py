"""ARCHITECTURE §9.3 demonstration workflow, steps 1–5, entirely through the API.

Later steps (6 payments … 11 anomalies) are added by the phase that builds them. The
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


def _create_documented_types_via_api(api, program_id):
    from scholarships.management.commands.seed_scholarship_types import SPECS

    for spec in sorted(SPECS, key=lambda s: s.scholarship_type_id):
        body = {
            k: (str(v) if hasattr(v, "quantize") else v)
            for k, v in vars(spec).items()
            if k != "scholarship_type_id"
        }
        body.update(
            effective_from="2026-01-01", program_ids=[program_id] if spec.program_restricted else []
        )
        created = _ok(api.post("/api/v1/scholarship-types/", body, format="json"), 201)
        assert created["scholarship_type_id"] == spec.scholarship_type_id


def test_workflow_steps_4_and_5(api, master_data):
    """§9.3 step 4: 7 results with reason codes. Step 5: award capped at eligible fees and
    applied; an over-cap request → 422."""
    _create_documented_types_via_api(api, master_data["program_id"])
    student = _ok(
        api.post(
            "/api/v1/students/",
            {
                "first_name": "Ravi",
                "last_name": "Iyer",
                "email": "ravi.iyer@example.edu",
                "date_of_birth": "2006-05-01",
                "residency": "DOMESTIC",
                "program": master_data["program_id"],
                "admit_term": "TERM-2026-FALL",
                "gpa": "3.72",
                "need_index": "0.45",
            },
            format="json",
        ),
        201,
    )
    sid = student["student_id"]
    enrollment = _ok(
        api.post(
            "/api/v1/enrollments/",
            {"student": sid, "term": "TERM-2026-FALL", "credits": 16, "enrolled_on": "2026-08-10"},
            format="json",
        ),
        201,
    )
    invoice = _ok(
        api.post(
            "/api/v1/invoices/generate/",
            {"enrollment_id": enrollment["enrollment_id"]},
            format="json",
        ),
        201,
    )

    # 4. eligibility check: one result per type, with reason codes
    results = _ok(
        api.post(
            "/api/v1/scholarships/eligibility-checks/",
            {"student_id": sid, "term_id": "TERM-2026-FALL"},
            format="json",
        ),
        201,
    )
    assert len(results) == 7
    by_type = {r["scholarship_type"]: r for r in results}
    assert {k for k, r in by_type.items() if r["is_eligible"]} == {"SCT-001", "SCT-004", "SCT-006"}
    assert by_type["SCT-007"]["reason_codes"] == ["GPA_BELOW_MIN", "NEED_BELOW_MIN"]
    assert all(r["reason_codes"] for r in results)

    # 5. award: computed and capped at the eligible fees; over-cap request → 422
    over = api.post(
        "/api/v1/scholarships/",
        {
            "eligibility_id": by_type["SCT-001"]["eligibility_id"],
            "invoice_id": invoice["invoice_id"],
            "requested_amount": "75000.01",
        },
        format="json",
    )
    assert over.status_code == 422
    assert over.json()["error"]["code"] == "SCHOLARSHIP_EXCEEDS_ELIGIBLE_FEES"
    award = _ok(
        api.post(
            "/api/v1/scholarships/",
            {
                "eligibility_id": by_type["SCT-001"]["eligibility_id"],
                "invoice_id": invoice["invoice_id"],
            },
            format="json",
        ),
        201,
    )
    assert award["awarded_amount"] == "75000.00"  # 50% of TUITION 144,000 + LAB 6,000
    applied = _ok(
        api.post(f"/api/v1/scholarships/{award['scholarship_id']}/apply/", {}, format="json"), 200
    )
    assert applied["status"] == "APPLIED" and applied["applied_amount"] == "75000.00"
    inv = _ok(api.get(f"/api/v1/invoices/{invoice['invoice_id']}/"), 200)
    assert (
        inv["amounts"]["scholarship_applied"],
        inv["amounts"]["outstanding"],
        inv["collection_status"],
    ) == (
        "75000.00",
        "84000.00",
        "UNPAID",
    )
    balance = _ok(api.get(f"/api/v1/students/{sid}/balance/"), 200)
    assert balance["ledger_balance"] == "84000.00" and balance["identity"]["holds"] is True
