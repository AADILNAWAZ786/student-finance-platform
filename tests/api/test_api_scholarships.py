"""Scholarships API: every endpoint, filters, strict input, error envelopes; every success
response validated against the served OpenAPI document."""

import pytest

pytestmark = pytest.mark.django_db

INV = "INV-STU-000001-2026-000001"
TYPE_BODY = {
    "name": "Science Bursary",
    "category": "BURSARY",
    "min_gpa": "3.00",
    "residency": "ALL",
    "first_term_only": False,
    "program_restricted": True,
    "program_ids": ["PRG-001"],
    "coverage_scope": "ELIGIBLE_LINES_ONLY",
    "award_basis": "FIXED",
    "award_value": "5000.00",
    "max_amount_per_term": "5000.00",
    "priority": 7,
    "effective_from": "2026-01-01",
}


def _error(r, status, code):
    assert r.status_code == status, r.content
    assert r.json()["error"]["code"] == code
    return r.json()["error"]


@pytest.fixture
def world(scholarship_types, invoice, finance_api):
    return finance_api


def _check(api):
    r = api.post(
        "/api/v1/scholarships/eligibility-checks/",
        {"student_id": "STU-000001", "term_id": "TERM-2026-FALL"},
        format="json",
    )
    assert r.status_code == 201, r.content
    return {e["scholarship_type"]: e for e in r.json()}


# --- types ------------------------------------------------------------------------------------


def test_list_and_create_types(world, assert_matches_openapi):
    r = world.get("/api/v1/scholarship-types/")
    assert_matches_openapi(r, "/api/v1/scholarship-types/", "get")
    assert [t["scholarship_type_id"] for t in r.json()["results"]][:2] == ["SCT-007", "SCT-001"]
    assert r.json()["results"][0]["award_value"] == "100.00"
    r = world.post("/api/v1/scholarship-types/", TYPE_BODY, format="json")
    assert r.status_code == 201, r.content
    assert_matches_openapi(r, "/api/v1/scholarship-types/", "post")
    assert (r.json()["scholarship_type_id"], r.json()["program_ids"]) == ("SCT-008", ["PRG-001"])
    assert world.get("/api/v1/scholarship-types/", {"category": "MERIT"}).json()["count"] == 2
    assert (
        world.get("/api/v1/scholarship-types/", {"coverage_scope": "ALL_FEE_LINES"}).json()["count"]
        == 1
    )


@pytest.mark.parametrize(
    ("patch", "field"),
    [
        ({"program_ids": []}, "program_ids"),
        ({"program_ids": ["PRG-404"]}, "program_ids"),
        ({"min_gpa": 3.0}, "min_gpa"),  # decimals are strings
        ({"award_value": "0.00"}, "award_value"),
        ({"category": "SPORTS"}, "category"),
        ({"scholarship_type_id": "SCT-099"}, "scholarship_type_id"),  # server-assigned
        ({"coverage_scope": "ALL_FEE_LINES"}, "category"),
    ],
)
def test_type_validation(world, patch, field):
    r = world.post("/api/v1/scholarship-types/", {**TYPE_BODY, **patch}, format="json")
    assert field in _error(r, 400, "VALIDATION_ERROR")["details"]


@pytest.mark.parametrize(
    ("patch", "errors"),
    [
        ({"effective_from": "2025-12-31"}, {"effective_from"}),
        ({"effective_from": "2027-01-01"}, {"effective_from"}),
        ({"effective_to": "2027-01-01"}, {"effective_to"}),
        (
            {"effective_from": "2025-01-01", "effective_to": "2025-12-31"},
            {"effective_from", "effective_to"},
        ),
    ],
)
def test_type_effective_dates_outside_2026_are_400(world, patch, errors):
    r = world.post("/api/v1/scholarship-types/", {**TYPE_BODY, **patch}, format="json")
    details = _error(r, 400, "VALIDATION_ERROR")["details"]
    assert set(details) == errors
    assert all(
        v == ["Must be a business date between 2026-01-01 and 2026-12-31."]
        for v in details.values()
    )


def test_type_max_gpa_zero_is_handled_consistently(world, assert_matches_openapi):
    r = world.post(
        "/api/v1/scholarship-types/",
        {**TYPE_BODY, "min_gpa": None, "max_gpa": "0.00"},
        format="json",
    )
    assert r.status_code == 201, r.content  # not a database 500
    assert_matches_openapi(r, "/api/v1/scholarship-types/", "post")
    assert r.json()["max_gpa"] == "0.00"
    r = world.post(
        "/api/v1/scholarship-types/",
        {**TYPE_BODY, "min_gpa": "0.00", "max_gpa": "0.00"},
        format="json",
    )
    assert _error(r, 400, "VALIDATION_ERROR")["details"] == {
        "max_gpa": ["max_gpa must be greater than min_gpa."]
    }
    r = world.post("/api/v1/scholarship-types/", {**TYPE_BODY, "max_gpa": "4.01"}, format="json")
    assert "max_gpa" in _error(r, 400, "VALIDATION_ERROR")["details"]


def test_no_type_or_eligibility_detail_routes(world):
    for path in ("/api/v1/scholarship-types/SCT-001/", "/api/v1/eligibility/ELG-2026-000001/"):
        assert _error(world.get(path), 404, "NOT_FOUND")


# --- eligibility --------------------------------------------------------------------------------


def test_eligibility_check_returns_every_type(world, assert_matches_openapi):
    r = world.post(
        "/api/v1/scholarships/eligibility-checks/",
        {"student_id": "STU-000001", "term_id": "TERM-2026-FALL"},
        format="json",
    )
    assert_matches_openapi(r, "/api/v1/scholarships/eligibility-checks/", "post")
    rows = {e["scholarship_type"]: e for e in r.json()}
    assert len(rows) == 7
    assert rows["SCT-001"]["reason_codes"] == ["ALL_CRITERIA_MET"]
    assert rows["SCT-001"]["evaluated_on"] == "2026-08-17"
    assert rows["SCT-001"]["eligible_fee_base"] == "150000.00"
    assert rows["SCT-007"]["eligible_fee_base"] == "164000.00"
    assert rows["SCT-005"]["reason_codes"] == ["RESIDENCY_MISMATCH"]


@pytest.mark.parametrize(
    ("body", "field"),
    [
        (
            {"student_id": "STU-000001", "term_id": "TERM-2026-FALL", "evaluated_on": "2026-09-01"},
            "evaluated_on",
        ),
        ({"student_id": "STU-999999", "term_id": "TERM-2026-FALL"}, "student_id"),
        ({"student_id": "STU-000001"}, "term_id"),
    ],
)
def test_D_S7_eligibility_check_validation(world, body, field):
    r = world.post("/api/v1/scholarships/eligibility-checks/", body, format="json")
    assert field in _error(r, 400, "VALIDATION_ERROR")["details"]


def test_eligibility_history_and_filters(world, assert_matches_openapi):
    _check(world)
    _check(world)
    r = world.get("/api/v1/eligibility/")
    assert_matches_openapi(r, "/api/v1/eligibility/", "get")
    assert r.json()["count"] == 14
    assert world.get("/api/v1/eligibility/", {"is_eligible": "true"}).json()["count"] == 4
    assert (
        world.get(
            "/api/v1/eligibility/", {"scholarship_type": "SCT-001", "student": "STU-000001"}
        ).json()["count"]
        == 2
    )
    r = world.get("/api/v1/eligibility/", {"evaluated_on": "2026-13-01"})
    assert _error(r, 400, "VALIDATION_ERROR")["details"] == {
        "evaluated_on": ["Enter a valid date."]
    }


# --- awards and application -----------------------------------------------------------------------


def test_award_apply_and_read(world, assert_matches_openapi):
    rows = _check(world)
    r = world.post(
        "/api/v1/scholarships/",
        {"eligibility_id": rows["SCT-001"]["eligibility_id"], "invoice_id": INV},
        format="json",
    )
    assert r.status_code == 201, r.content
    assert_matches_openapi(r, "/api/v1/scholarships/", "post")
    award = r.json()
    assert (award["awarded_amount"], award["applied_amount"], award["status"]) == (
        "75000.00",
        "0.00",
        "OFFERED",
    )
    r = world.post(f"/api/v1/scholarships/{award['scholarship_id']}/apply/", {}, format="json")
    assert r.status_code == 200, r.content
    assert_matches_openapi(r, "/api/v1/scholarships/{scholarship_id}/apply/", "post")
    body = r.json()
    assert (body["status"], body["applied_amount"]) == ("APPLIED", "75000.00")
    assert [e["to_status"] for e in body["events"]] == ["OFFERED", "ACCEPTED", "APPLIED"]
    assert body["allocations"][0] == {
        "allocation_id": "SCHA-SCH-2026-000001-01",
        "scholarship": "SCH-2026-000001",
        "invoice_line": f"{INV}-L01",
        "allocation_type": "APPLY",
        "amount": "75000.00",
        "reverses": None,
        "reason": "AWARD",
        "allocated_on": "2026-08-17",
    }
    r = world.get("/api/v1/scholarships/SCH-2026-000001/")
    assert_matches_openapi(r, "/api/v1/scholarships/{scholarship_id}/", "get")
    r = world.get("/api/v1/scholarships/", {"status": "APPLIED", "invoice": INV})
    assert_matches_openapi(r, "/api/v1/scholarships/", "get")
    assert r.json()["count"] == 1

    inv = world.get(f"/api/v1/invoices/{INV}/").json()
    assert inv["amounts"]["scholarship_applied"] == "75000.00"
    assert inv["amounts"]["amount_paid"] == "0.00"  # scholarship is never money paid
    assert inv["lines"][0]["open_amount"] == "69000.00"
    ledger = world.get("/api/v1/students/STU-000001/ledger/").json()["results"]
    credit = [e for e in ledger if e["entry_type"] == "SCHOLARSHIP_CREDIT"][0]
    assert (
        credit["amount"],
        credit["source_type"],
        credit["scholarship_allocation"],
        credit["invoice"],
    ) == (
        "-75000.00",
        "SCHOLARSHIP_ALLOCATION",
        "SCHA-SCH-2026-000001-01",
        INV,
    )
    statement = world.get("/api/v1/students/STU-000001/statement/").json()
    assert statement["invoices"][0]["scholarship_allocations"][0]["amount"] == "75000.00"
    balance = world.get("/api/v1/students/STU-000001/balance/").json()
    assert balance["breakdown"]["scholarship_credits"] == "-75000.00"
    assert balance["identity"]["holds"] is True


@pytest.mark.parametrize(
    ("extra", "status", "code"),
    [
        ({"requested_amount": "0.00"}, 400, "VALIDATION_ERROR"),
        ({"requested_amount": "-5.00"}, 400, "VALIDATION_ERROR"),
        ({"requested_amount": 100}, 400, "VALIDATION_ERROR"),  # money is a string
        ({"requested_amount": "75000.01"}, 422, "SCHOLARSHIP_EXCEEDS_ELIGIBLE_FEES"),
        ({"award_date": "2026-08-16"}, 400, "VALIDATION_ERROR"),
        ({"status": "APPLIED"}, 400, "VALIDATION_ERROR"),
    ],
)
def test_award_validation(world, extra, status, code):
    rows = _check(world)
    body = {"eligibility_id": rows["SCT-001"]["eligibility_id"], "invoice_id": INV, **extra}
    _error(world.post("/api/v1/scholarships/", body, format="json"), status, code)


def test_award_errors(world):
    rows = _check(world)
    r = world.post(
        "/api/v1/scholarships/",
        {"eligibility_id": rows["SCT-002"]["eligibility_id"], "invoice_id": INV},
        format="json",
    )
    assert _error(r, 422, "SCHOLARSHIP_NOT_ELIGIBLE")["rule_id"] == "BR-SCH-02"
    r = world.post(
        "/api/v1/scholarships/",
        {"eligibility_id": "ELG-2026-999999", "invoice_id": INV},
        format="json",
    )
    assert "eligibility_id" in _error(r, 400, "VALIDATION_ERROR")["details"]


def test_apply_errors(world):
    rows = _check(world)
    world.post(
        "/api/v1/scholarships/",
        {"eligibility_id": rows["SCT-001"]["eligibility_id"], "invoice_id": INV},
        format="json",
    )
    r = world.post(
        "/api/v1/scholarships/SCH-2026-000001/apply/", {"allocated_on": "2026-08-16"}, format="json"
    )
    assert "allocated_on" in _error(r, 400, "VALIDATION_ERROR")["details"]
    r = world.post("/api/v1/scholarships/SCH-2026-000001/apply/", {"amount": "1.00"}, format="json")
    assert "amount" in _error(r, 400, "VALIDATION_ERROR")["details"]
    world.post("/api/v1/scholarships/SCH-2026-000001/apply/", {}, format="json")
    _error(
        world.post("/api/v1/scholarships/SCH-2026-000001/apply/", {}, format="json"),
        409,
        "ILLEGAL_TRANSITION",
    )
    r = world.post("/api/v1/scholarships/SCH-2026-999999/apply/", {}, format="json")
    assert _error(r, 404, "NOT_FOUND")["message"] == "No Scholarship matches the given query."


def test_full_ride_through_the_api_is_scholarship_covered(
    scholarship_types, student_with_invoice, finance_api
):
    s, inv = student_with_invoice(gpa="3.90", need_index="0.90")
    rows = {
        e["scholarship_type"]: e
        for e in finance_api.post(
            "/api/v1/scholarships/eligibility-checks/",
            {"student_id": s.pk, "term_id": "TERM-2026-FALL"},
            format="json",
        ).json()
    }
    award = finance_api.post(
        "/api/v1/scholarships/",
        {"eligibility_id": rows["SCT-007"]["eligibility_id"], "invoice_id": inv.pk},
        format="json",
    ).json()
    assert award["awarded_amount"] == "164000.00"
    finance_api.post(f"/api/v1/scholarships/{award['scholarship_id']}/apply/", {}, format="json")
    body = finance_api.get(f"/api/v1/invoices/{inv.pk}/").json()
    assert (
        body["collection_status"],
        body["amounts"]["outstanding"],
        body["amounts"]["amount_paid"],
    ) == (
        "SCHOLARSHIP_COVERED",
        "0.00",
        "0.00",
    )
    r = finance_api.post(
        "/api/v1/scholarships/",
        {"eligibility_id": rows["SCT-001"]["eligibility_id"], "invoice_id": inv.pk},
        format="json",
    )
    assert _error(r, 409, "SCHOLARSHIP_GROUP_CONFLICT")["rule_id"] == "BR-SCH-08"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("put", "/api/v1/scholarships/SCH-2026-000001/"),
        ("patch", "/api/v1/scholarships/SCH-2026-000001/"),
        ("delete", "/api/v1/scholarships/SCH-2026-000001/"),
        ("put", "/api/v1/scholarship-types/"),
        ("delete", "/api/v1/eligibility/"),
        ("get", "/api/v1/scholarships/SCH-2026-000001/apply/"),
        ("get", "/api/v1/scholarships/eligibility-checks/"),
    ],
)
def test_read_and_action_only(world, method, path):
    rows = _check(world)
    world.post(
        "/api/v1/scholarships/",
        {"eligibility_id": rows["SCT-001"]["eligibility_id"], "invoice_id": INV},
        format="json",
    )
    r = getattr(world, method)(path, {}, format="json")
    assert r.status_code == 405 and r.json()["error"]["code"] == "METHOD_NOT_ALLOWED"


def test_money_and_decimals_are_strings(world):
    rows = _check(world)
    world.post(
        "/api/v1/scholarships/",
        {"eligibility_id": rows["SCT-001"]["eligibility_id"], "invoice_id": INV},
        format="json",
    )
    bodies = [
        world.get(p).json()
        for p in (
            "/api/v1/scholarship-types/",
            "/api/v1/eligibility/",
            "/api/v1/scholarships/SCH-2026-000001/",
        )
    ]

    def floats(node):
        if isinstance(node, dict):
            return [x for v in node.values() for x in floats(v)]
        if isinstance(node, list):
            return [x for v in node for x in floats(v)]
        return [node] if isinstance(node, float) else []

    assert [x for b in bodies for x in floats(b)] == []


def test_business_dates_never_come_from_the_clock(world, monkeypatch):
    """D-S7/D-S9: evaluated_on, award_date and allocated_on are derived from stored dates.
    (``recorded_at`` is execution metadata and does use the clock, by design.)"""
    from pathlib import Path

    import core.dates

    monkeypatch.setattr(core.dates, "business_today", lambda: (_ for _ in ()).throw(AssertionError))
    source = "\n".join(
        p.read_text() for p in Path("scholarships").rglob("*.py") if "migrations" not in p.parts
    )
    assert not any(
        word in source for word in ("business_today", "date.today", "localdate", "timezone.now")
    )
    rows = _check(world)
    r = world.post(
        "/api/v1/scholarships/",
        {"eligibility_id": rows["SCT-001"]["eligibility_id"], "invoice_id": INV},
        format="json",
    )
    r = world.post(f"/api/v1/scholarships/{r.json()['scholarship_id']}/apply/", {}, format="json")
    assert (r.json()["award_date"], r.json()["allocations"][0]["allocated_on"]) == (
        "2026-08-17",
        "2026-08-17",
    )
